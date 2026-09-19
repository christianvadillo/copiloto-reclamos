"""meli/client.py — cliente síncrono del subconjunto de la API de Mercado Libre que usamos.

Cada método corresponde a una fila verificada de la tabla §3 de `docs/ESPECIFICACION.md`.
Deliberadamente NO decide nada de negocio: solo hace la llamada, reintenta lo transitorio y
devuelve JSON crudo (dict). La traducción a nuestro vocabulario vive en `taxonomy.py` y en
`pipeline.py`.

`http_client` es inyectable para que los tests (y `copiloto demo`) apunten el mismo código a
`meli.fake` en vez de a la red real — nunca hay una rama "modo test" dentro de esta clase.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from copiloto.meli.oauth import TokenProvider

_CLAIM_ID_RE = re.compile(r"claims/(\d+)")


def extract_claim_id(resource: str | None) -> str | None:
    """`/v1/claims/123` o `/post-purchase/v1/claims/123` → `"123"`. El webhook nunca confía en
    más que esto del payload de la notificación (ver ESPECIFICACION.md §6)."""
    if not resource:
        return None
    m = _CLAIM_ID_RE.search(resource)
    return m.group(1) if m else None


class MeliError(Exception):
    """Cualquier respuesta de error de la API, ya reintentada si aplicaba.

    `uncertain=True` significa que NO sabemos si Mercado Libre aplicó la operación (se cortó la
    conexión después de enviar un POST, o respondió 5xx): quien ejecuta acciones de dinero debe
    tratarlo como "en duda" y NO repetir la llamada a ciegas."""

    def __init__(self, status: int, body: str, uncertain: bool = False) -> None:
        super().__init__(f"Mercado Libre respondió {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.uncertain = uncertain


def _json_or_empty(resp: httpx.Response) -> dict:
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


class MeliClient:
    """Un vendedor por llamada (`seller_id`): el `TokenProvider` resuelve y refresca su token."""

    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        http_client: httpx.Client | None = None,
        max_retries: int = 4,
        timeout: float = 15.0,
        backoff_base: float = 0.5,
        sleep_fn: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout)
        self.max_retries = max_retries
        self.timeout = timeout
        self.backoff_base = backoff_base
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._rng = rng if rng is not None else random.Random()

    def _backoff_seconds(self, attempt: int) -> float:
        return self.backoff_base * (2 ** (attempt - 1)) + self._rng.uniform(0, self.backoff_base)

    def _request(
        self,
        method: str,
        path: str,
        *,
        seller_id: str | None = None,
        access_token: str | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
        files: dict | None = None,
        extra_headers: dict | None = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        """GET se reintenta ante cualquier falla transitoria. Un POST (mensaje, reembolso) solo se
        reintenta si es seguro que ML no lo procesó: 429 o error de CONEXIÓN (la petición no salió).
        Un timeout de lectura o un 5xx en POST se reportan como `uncertain` en vez de repetirse:
        repetir un reembolso parcial es peor que pedir una revisión manual."""
        if idempotent is None:
            idempotent = method.upper() == "GET"
        token = access_token if access_token is not None else self.token_provider.get(seller_id)  # type: ignore[arg-type]
        headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            try:
                # El timeout se configura una sola vez al construir `self._http` (ver
                # `__init__`): pasarlo también aquí hace que `TestClient` (usado en tests y en
                # `copiloto demo`) emita un `DeprecationWarning` en cada llamada.
                resp = self._http.request(method, url, headers=headers, params=params, json=json_body, files=files)
            except httpx.TransportError as exc:
                never_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if not (idempotent or never_sent):
                    raise MeliError(0, f"error de transporte tras enviar: {exc}", uncertain=True) from exc
                attempt += 1
                if attempt > self.max_retries:
                    raise MeliError(0, f"error de transporte: {exc}", uncertain=not never_sent) from exc
                self._sleep(self._backoff_seconds(attempt))
                continue
            if resp.status_code in self.RETRYABLE_STATUS:
                if not idempotent and resp.status_code != 429:
                    raise MeliError(resp.status_code, resp.text, uncertain=True)
                attempt += 1
                if attempt > self.max_retries:
                    raise MeliError(resp.status_code, resp.text, uncertain=resp.status_code != 429 and not idempotent)
                self._sleep(self._backoff_seconds(attempt))
                continue
            if resp.status_code >= 400:
                raise MeliError(resp.status_code, resp.text)
            return resp

    def _get(self, path: str, seller_id: str, params: dict | None = None, extra_headers: dict | None = None) -> dict:
        return _json_or_empty(
            self._request("GET", path, seller_id=seller_id, params=params, extra_headers=extra_headers)
        )

    def _post(self, path: str, seller_id: str, json_body: dict | None = None) -> dict:
        return _json_or_empty(self._request("POST", path, seller_id=seller_id, json_body=json_body))

    # ── Autenticación cruda (usada por el callback de OAuth antes de tener un Seller) ──────

    def whoami(self, access_token: str) -> dict:
        return _json_or_empty(self._request("GET", "/users/me", access_token=access_token))

    # ── Reclamos ────────────────────────────────────────────────────────────────────────

    def get_claim(self, seller_id: str, claim_id: str) -> dict:
        return self._get(f"/post-purchase/v1/claims/{claim_id}", seller_id)

    def get_claim_detail(self, seller_id: str, claim_id: str) -> dict:
        return self._get(f"/post-purchase/v1/claims/{claim_id}/detail", seller_id)

    def get_claim_reason(self, seller_id: str, reason_id: str) -> dict:
        return self._get(f"/post-purchase/v1/claims/reasons/{reason_id}", seller_id)

    def get_expected_resolutions(self, seller_id: str, claim_id: str) -> dict:
        return self._get(f"/post-purchase/v1/claims/{claim_id}/expected-resolutions", seller_id)

    def get_partial_refund_offers(self, seller_id: str, claim_id: str) -> dict:
        return self._get(f"/post-purchase/v1/claims/{claim_id}/partial-refund/available-offers", seller_id)

    def get_affects_reputation(self, seller_id: str, claim_id: str) -> dict:
        return self._get(f"/post-purchase/v1/claims/{claim_id}/affects-reputation", seller_id)

    def get_messages(self, seller_id: str, claim_id: str) -> list[dict]:
        body = self._get(f"/post-purchase/v1/claims/{claim_id}/messages", seller_id)
        return body if isinstance(body, list) else body.get("messages", [])

    def send_message(
        self, seller_id: str, claim_id: str, receiver_role: str, message: str, attachments: list[str] | None = None
    ) -> dict:
        body: dict[str, Any] = {"receiver_role": receiver_role, "message": message}
        if attachments:
            body["attachments"] = attachments
        return self._post(f"/post-purchase/v1/claims/{claim_id}/actions/send-message", seller_id, body)

    def upload_attachment(
        self, seller_id: str, claim_id: str, filename: str, content: bytes, content_type: str
    ) -> dict:
        files = {"file": (filename, content, content_type)}
        resp = self._request(
            "POST", f"/post-purchase/v1/claims/{claim_id}/attachments", seller_id=seller_id, files=files
        )
        return _json_or_empty(resp)

    def refund(self, seller_id: str, claim_id: str) -> dict:
        return self._post(f"/post-purchase/v1/claims/{claim_id}/expected-resolutions/refund", seller_id)

    def allow_return(self, seller_id: str, claim_id: str) -> dict:
        return self._post(f"/post-purchase/v1/claims/{claim_id}/expected-resolutions/allow-return", seller_id)

    def partial_refund(self, seller_id: str, claim_id: str, percentage: int) -> dict:
        return self._post(
            f"/post-purchase/v1/claims/{claim_id}/expected-resolutions/partial-refund",
            seller_id,
            {"percentage": percentage},
        )

    def open_dispute(self, seller_id: str, claim_id: str) -> dict:
        # No lo usamos en el flujo (defendemos por mensaje, no abrimos mediación nosotros);
        # se expone porque está en la API verificada y puede servir en el módulo 2.
        return self._post(f"/post-purchase/v1/claims/{claim_id}/actions/open-dispute", seller_id)

    def get_status_history(self, seller_id: str, claim_id: str) -> list[dict]:
        body = self._get(f"/post-purchase/v1/claims/{claim_id}/status-history", seller_id)
        return body if isinstance(body, list) else (body.get("data") or body.get("results") or [])

    def get_actions_history(self, seller_id: str, claim_id: str) -> list[dict]:
        body = self._get(f"/post-purchase/v1/claims/{claim_id}/actions-history", seller_id)
        return body if isinstance(body, list) else (body.get("data") or body.get("results") or [])

    def get_returns(self, seller_id: str, claim_id: str) -> dict:
        return self._get(f"/post-purchase/v2/claims/{claim_id}/returns", seller_id)

    def search_claims(
        self,
        seller_id: str,
        *,
        player_role: str = "respondent",
        player_user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {"players.role": player_role, "limit": min(limit, 100), "offset": offset}
        if player_user_id is not None:
            params["players.user_id"] = player_user_id
        if status is not None:
            params["status"] = status
        return self._get("/post-purchase/v1/claims/search", seller_id, params=params)

    # ── Órdenes y envíos ────────────────────────────────────────────────────────────────

    def get_order(self, seller_id: str, order_id: str) -> dict:
        return self._get(f"/orders/{order_id}", seller_id)

    def get_shipment(self, seller_id: str, shipment_id: str) -> dict:
        return self._get(f"/shipments/{shipment_id}", seller_id, extra_headers={"x-format-new": "true"})

    def get_shipment_history(self, seller_id: str, shipment_id: str) -> list[dict]:
        body = self._get(f"/shipments/{shipment_id}/history", seller_id)
        return body if isinstance(body, list) else body.get("history", [])

    # ── Usuario / reputación ────────────────────────────────────────────────────────────

    def get_user(self, seller_id: str, user_id: str) -> dict:
        return self._get(f"/users/{user_id}", seller_id)

    # ── Notificaciones perdidas ─────────────────────────────────────────────────────────

    def get_missed_feeds(self, seller_id: str, app_id: str, topic: str = "post_purchase") -> list[dict]:
        body = self._get("/missed_feeds", seller_id, params={"app_id": app_id, "topic": topic})
        return body if isinstance(body, list) else (body.get("data") or body.get("results") or [])

"""meli/oauth.py — autorización OAuth2 de Mercado Libre y rotación de tokens.

Dos cosas verificadas en la especificación que son fáciles de romper si no se respetan:
1. `expires_in` real es 10800 s (3 h), no las 6 h que dice la prosa de ML: siempre se usa el
   valor que llega en la respuesta, nunca una constante.
2. El refresh token es de **un solo uso**: al canjearlo, ML invalida el viejo y entrega uno
   nuevo. Si el proceso muere después de usar el refresh token pero antes de guardar el
   nuevo, el vendedor queda bloqueado (tendría que re-autorizar a mano). Por eso
   `TokenProvider.get()` persiste el par nuevo ANTES de devolver el access token al llamador.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from copiloto.config import Settings
    from copiloto.store import Store


@dataclass(frozen=True)
class PKCEPair:
    verifier: str
    challenge: str


def generate_pkce_pair() -> PKCEPair:
    """S256: verifier aleatorio de 43-128 chars, challenge = base64url(sha256(verifier))."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return PKCEPair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def authorization_url(settings: Settings, state: str, code_challenge: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.app_id,
        "redirect_uri": settings.redirect_uri,
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{settings.auth_base_url}/authorization?{urlencode(params)}"


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    user_id: str | None = None
    scope: str | None = None

    @property
    def expires_at(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.expires_in)


class InvalidGrantError(Exception):
    """El `code` o el `refresh_token` ya no sirven (expirado, reusado o revocado)."""


def _parse_token_response(body: dict) -> TokenResponse:
    return TokenResponse(
        access_token=body["access_token"],
        token_type=body.get("token_type", "bearer"),
        expires_in=int(body.get("expires_in", 21_600)),
        refresh_token=body.get("refresh_token", ""),
        user_id=(str(body["user_id"]) if body.get("user_id") is not None else None),
        scope=body.get("scope"),
    )


def _post_token(http: httpx.Client, settings: Settings, data: dict) -> TokenResponse:
    resp = http.post(f"{settings.meli_base_url}/oauth/token", data=data, headers={"Accept": "application/json"})
    if resp.status_code == 400 and "invalid_grant" in resp.text:
        raise InvalidGrantError(resp.text[:300])
    resp.raise_for_status()
    return _parse_token_response(resp.json())


def exchange_code(http: httpx.Client, settings: Settings, code: str, code_verifier: str | None = None) -> TokenResponse:
    data = {
        "grant_type": "authorization_code",
        "client_id": settings.app_id,
        "client_secret": settings.client_secret,
        "code": code,
        "redirect_uri": settings.redirect_uri,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    return _post_token(http, settings, data)


def refresh_token(http: httpx.Client, settings: Settings, refresh_token_value: str) -> TokenResponse:
    data = {
        "grant_type": "refresh_token",
        "client_id": settings.app_id,
        "client_secret": settings.client_secret,
        "refresh_token": refresh_token_value,
    }
    return _post_token(http, settings, data)


def fetch_me(http: httpx.Client, settings: Settings, access_token: str) -> dict:
    resp = http.get(f"{settings.meli_base_url}/users/me", headers={"Authorization": f"Bearer {access_token}"})
    resp.raise_for_status()
    return resp.json()


class NeedsReauthError(Exception):
    def __init__(self, seller_id: str) -> None:
        super().__init__(f"el vendedor {seller_id} necesita volver a autorizar la aplicación")
        self.seller_id = seller_id


class TokenProvider:
    """`get(seller_id)` siempre devuelve un access token vigente. Refresca si faltan menos de
    `REFRESH_MARGIN`; si ML responde `invalid_grant`, marca al vendedor `needs_reauth` (no hay
    forma automática de recuperarse: hace falta que vuelva a pasar por `/oauth/start`)."""

    REFRESH_MARGIN = timedelta(minutes=5)

    def __init__(
        self,
        store: Store,
        settings: Settings,
        http_client: httpx.Client | None = None,
        now_fn=None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._http = http_client if http_client is not None else httpx.Client(timeout=15.0)
        self._now = now_fn or (lambda: datetime.now(UTC))

    def get(self, seller_id: str) -> str:
        seller = self.store.get_seller(seller_id)
        if seller is None:
            raise LookupError(f"vendedor desconocido: {seller_id}")
        if seller.needs_reauth:
            raise NeedsReauthError(seller_id)
        if seller.token_expires_at - self._now() > self.REFRESH_MARGIN:
            return seller.access_token
        try:
            tokens = refresh_token(self._http, self.settings, seller.refresh_token)
        except InvalidGrantError:
            self.store.mark_needs_reauth(seller_id)
            raise NeedsReauthError(seller_id) from None
        # Persistir ANTES de devolver: en cuanto ML emitió este refresh token, el anterior
        # quedó inválido — si no lo guardamos ahora, la próxima llamada ya no podrá refrescar.
        self.store.update_tokens(seller_id, tokens.access_token, tokens.refresh_token, tokens.expires_at)
        return tokens.access_token

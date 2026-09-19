"""app.py — servicio FastAPI: webhook de notificaciones, OAuth y dashboard.

El webhook es la parte más sensible en tiempo: Mercado Libre reintenta ~8 veces en 1 hora y
**desactiva el tópico** si no respondemos rápido (ESPECIFICACION.md §6), así que
`POST /notifications` NUNCA llama a la API de Mercado Libre — valida lo mínimo (con qué app
habla, opcionalmente desde qué IP), deduplica por `(topic, resource, sent)` y encola un job.
Todo lo que de verdad cuesta tiempo (traer el reclamo, clasificar, recomendar, redactar) pasa
en `worker.py`, en otro proceso, leyendo la misma base SQLite.

El dashboard es deliberadamente plano: Jinja2 puro, sin JS externo ni build, porque quien lo
usa es el vendedor decidiendo si aprueba un mensaje que va a salir con su nombre — cuanta menos
magia en el camino entre "leer" y "aprobar", mejor.
"""

from __future__ import annotations

import html
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from copiloto.config import Settings
from copiloto.domain import Action
from copiloto.drafting.guardrails import check_message
from copiloto.meli.client import extract_claim_id
from copiloto.meli.oauth import authorization_url, exchange_code, fetch_me, generate_state
from copiloto.store import Store

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"

# Las 8 IPs emisoras verificadas en ESPECIFICACION.md §6 (el webhook no tiene firma HMAC: esto
# es opcional y complementario a validar `application_id`, nunca el único candado).
ML_NOTIFICATION_IPS = frozenset(
    {
        "54.88.218.97",
        "18.215.140.160",
        "18.213.114.129",
        "18.206.34.84",
        "35.236.253.169",
        "35.245.91.34",
        "35.245.20.104",
        "35.186.182.146",
    }
)
_CLAIM_TOPICS = frozenset({"claims", "claims_actions"})


def _client_ip(request: Request, trusted_proxy: bool) -> str | None:
    if trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _countdown(claim: dict, now: datetime) -> tuple[str, str]:
    """Texto + clase CSS para la cuenta regresiva del dashboard: vencimiento de la ventana de
    48 h si aplica, si no el `due_date` de la acción del respondent."""
    due_raw = claim.get("incentive_due_at") or claim.get("action_due_at")
    if not due_raw:
        return "sin vencimiento", ""
    try:
        due = datetime.fromisoformat(due_raw)
    except ValueError:
        return "sin vencimiento", ""
    hours = (due - now).total_seconds() / 3600
    if hours < 0:
        return f"vencido hace {abs(hours):.1f} h", "urgent"
    if hours < 6:
        return f"vence en {hours:.1f} h", "urgent"
    if hours < 24:
        return f"vence en {hours:.1f} h", "soon"
    return f"vence en {hours:.1f} h", "ok"


def create_app(settings: Settings, store: Store | None = None, http_client: httpx.Client | None = None) -> FastAPI:
    """`store`/`http_client` inyectables: en producción se construyen aquí; en tests y en
    `copiloto demo` los pasa el llamador ya apuntando al simulador."""
    store = store if store is not None else Store(settings.db_path, settings.secret_key)
    http_client = http_client if http_client is not None else httpx.Client(timeout=15.0)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    security = HTTPBasic(auto_error=False)
    oauth_states: set[str] = set()  # `state` emitidos por /oauth/start, pendientes de callback

    app = FastAPI(title="Copiloto de reclamos")

    # OJO: `credentials` usa el estilo viejo `= Depends(security)` (no `Annotated[...,
    # Depends(security)]`) a propósito. `security` es una variable local de `create_app` y este
    # módulo tiene `from __future__ import annotations`: con `Annotated` en la anotación, FastAPI
    # intenta resolver el `Depends(security)` a partir del string de la anotación usando SOLO los
    # globals del módulo (nunca los locals de la función envolvente), no encuentra `security` y
    # calla el error — el parámetro se interpreta como un query param normal y la auth queda
    # rota en silencio (siempre 401 con credenciales correctas). Con `Depends(...)` como valor
    # por defecto, se evalúa de inmediato (no se stringifica) y sí ve el closure.
    def require_dashboard_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:  # noqa: B008
        if not settings.dashboard_user:
            return
        ok = (
            credentials is not None
            and secrets.compare_digest(credentials.username, settings.dashboard_user)
            and secrets.compare_digest(credentials.password, settings.dashboard_password or "")
        )
        if not ok:
            raise HTTPException(status_code=401, detail="credenciales inválidas", headers={"WWW-Authenticate": "Basic"})

    # ── Salud ───────────────────────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "mode": settings.mode}

    # ── Webhook ─────────────────────────────────────────────────────────────────────────

    @app.post("/notifications")
    def receive_notification(request: Request, payload: dict = Body(...)) -> dict:  # noqa: B008 (patrón estándar de FastAPI)
        application_id = str(payload.get("application_id", ""))
        if settings.app_id and application_id != str(settings.app_id):
            logger.warning("notificación con application_id ajeno: %s", application_id)
            store.add_event(None, None, "webhook_rejected_app_id", {"application_id": application_id})
            raise HTTPException(status_code=403, detail="application_id no reconocido")
        if settings.ip_allowlist_enabled:
            ip = _client_ip(request, settings.trusted_proxy)
            if ip not in ML_NOTIFICATION_IPS:
                logger.warning("notificación desde IP no permitida: %s", ip)
                store.add_event(None, None, "webhook_rejected_ip", {"ip": ip})
                raise HTTPException(status_code=403, detail="origen no permitido")

        topic = str(payload.get("topic", ""))
        resource = str(payload.get("resource", ""))
        sent = str(payload.get("sent", ""))
        user_id = str(payload.get("user_id", ""))
        claim_id = extract_claim_id(resource)
        dedupe_key = f"{topic}|{resource}|{sent}"
        is_new = store.save_notification(dedupe_key, topic, resource, claim_id, user_id, application_id)
        if is_new and topic in _CLAIM_TOPICS and claim_id and user_id:
            store.enqueue_job("process_claim", {"seller_id": user_id, "claim_id": claim_id})
        elif not is_new:
            logger.debug("notificación duplicada ignorada: %s", dedupe_key)
        return {"status": "ok"}

    # ── Calculadora pública (sin login): el imán de prospección ────────────────────────

    @app.get("/calculadora", response_class=HTMLResponse)
    def calculadora() -> HTMLResponse:
        """Página estática y autocontenida (el mismo modelo de λ en JavaScript). Se publica tal
        cual como Artifact; aquí se envuelve en su esqueleto HTML para servirla directo."""
        page = (Path(__file__).parent / "web" / "calculadora.html").read_text(encoding="utf-8")
        split = page.index('<div class="wrap">')
        return HTMLResponse(
            '<!doctype html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f"{page[:split]}</head><body>{page[split:]}</body></html>"
        )

    # ── OAuth ───────────────────────────────────────────────────────────────────────────

    @app.get("/oauth/start")
    def oauth_start() -> RedirectResponse:
        state = generate_state()
        oauth_states.add(state)
        return RedirectResponse(authorization_url(settings, state))

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> HTMLResponse:
        if error:
            raise HTTPException(status_code=400, detail=f"Mercado Libre devolvió un error: {error}")
        if not code or not state or state not in oauth_states:
            raise HTTPException(status_code=400, detail="state inválido, expirado o ausente")
        oauth_states.discard(state)
        tokens = exchange_code(http_client, settings, code)
        me = fetch_me(http_client, settings, tokens.access_token)
        seller_id = str(me.get("id") or tokens.user_id)
        store.upsert_seller(seller_id, me.get("nickname"), tokens.access_token, tokens.refresh_token, tokens.expires_at)
        store.add_event(None, seller_id, "oauth_authorized", {"nickname": me.get("nickname")})
        nickname = html.escape(str(me.get("nickname", seller_id)))
        return HTMLResponse(
            f"<h1>Cuenta conectada</h1><p>{nickname} ({html.escape(seller_id)}) ya puede recibir reclamos.</p>"
        )

    # ── Dashboard ───────────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def dashboard_list(request: Request, _auth: None = Depends(require_dashboard_auth)) -> HTMLResponse:
        now = datetime.now(UTC)
        rows = []
        for claim in store.list_open_claims():
            rec = store.get_latest_recommendation(claim["claim_id"])
            text, css = _countdown(claim, now)
            rows.append({"claim": claim, "rec": rec, "countdown": text, "countdown_class": css})
        return templates.TemplateResponse(request, "list.html", {"claims": rows, "mode": settings.mode})

    @app.get("/claims/{claim_id}", response_class=HTMLResponse)
    def dashboard_detail(
        claim_id: str, request: Request, _auth: None = Depends(require_dashboard_auth)
    ) -> HTMLResponse:
        claim = store.get_claim_row(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="reclamo no encontrado")
        rec = store.get_latest_recommendation(claim_id)
        draft = store.get_latest_draft(claim_id)
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "claim": claim,
                "rec": rec,
                "draft": draft,
                "mode": settings.mode,
                "max_chars": settings.message_max_chars,
            },
        )

    @app.post("/claims/{claim_id}/approve", response_model=None)
    def approve_claim(
        claim_id: str,
        request: Request,
        message: Annotated[str, Form()],
        _auth: None = Depends(require_dashboard_auth),
    ) -> Response:
        claim = store.get_claim_row(claim_id)
        rec = store.get_latest_recommendation(claim_id)
        draft = store.get_latest_draft(claim_id)
        if claim is None or rec is None:
            raise HTTPException(status_code=404, detail="reclamo sin recomendación que aprobar")

        action = Action(rec["action"])
        violations = check_message(message, action, rec.get("params") or {}, settings.message_max_chars)
        if violations:
            return templates.TemplateResponse(
                request,
                "detail.html",
                {
                    "claim": claim,
                    "rec": rec,
                    "draft": draft,
                    "mode": settings.mode,
                    "max_chars": settings.message_max_chars,
                    "violations": violations,
                    "edited_message": message,
                },
                status_code=400,
            )

        approval_id = store.create_approval(
            claim_id=claim_id,
            seller_id=claim["seller_id"],
            recommendation_id=rec["id"],
            draft_id=draft["id"] if draft else None,
            action=rec["action"],
            params=rec.get("params") or {},
            decision="approved",
            edited_message=message,
            approved_by=settings.dashboard_user or "vendedor",
        )
        store.add_event(claim_id, claim["seller_id"], "approved", {"action": rec["action"], "approval_id": approval_id})
        store.enqueue_job("execute", {"approval_id": approval_id})
        return RedirectResponse(f"/claims/{claim_id}", status_code=303)

    @app.post("/claims/{claim_id}/reject")
    def reject_claim(claim_id: str, _auth: None = Depends(require_dashboard_auth)) -> RedirectResponse:
        claim = store.get_claim_row(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="reclamo no encontrado")
        rec = store.get_latest_recommendation(claim_id)
        store.create_approval(
            claim_id=claim_id,
            seller_id=claim["seller_id"],
            recommendation_id=rec["id"] if rec else None,
            draft_id=None,
            action=rec["action"] if rec else "",
            params={},
            decision="rejected",
            edited_message=None,
            approved_by=settings.dashboard_user or "vendedor",
        )
        store.add_event(claim_id, claim["seller_id"], "rejected", {})
        return RedirectResponse(f"/claims/{claim_id}", status_code=303)

    # ── API JSON ────────────────────────────────────────────────────────────────────────

    @app.get("/api/claims")
    def api_claims(_auth: None = Depends(require_dashboard_auth)) -> list[dict]:
        return store.list_claims()

    @app.get("/api/claims/{claim_id}")
    def api_claim_detail(claim_id: str, _auth: None = Depends(require_dashboard_auth)) -> dict:
        claim = store.get_claim_row(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="reclamo no encontrado")
        return {
            "claim": claim,
            "recommendation": store.get_latest_recommendation(claim_id),
            "draft": store.get_latest_draft(claim_id),
        }

    return app

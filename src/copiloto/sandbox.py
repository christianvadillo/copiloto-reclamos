"""sandbox.py — el copiloto completo contra una API de Mercado Libre simulada, para probarlo a mano.

`copiloto demo` prueba el flujo de corrido y termina. El sandbox deja el servicio vivo: el
dashboard real (el mismo de producción), un worker en segundo plano y una consola `/sandbox`
donde TÚ haces de comprador y de Mercado Libre — abrir reclamos nuevos, aceptar o rechazar una
oferta, escalar a mediación, que ML falle a favor de uno u otro — y ves cómo el copiloto
recomienda, redacta, ejecuta y aprende del cierre (los `Outcome` alimentan los priors).

Nada sale a la red salvo que pidas `--llm`: entonces los borradores los escribe Claude con tu
llave (cuesta centavos por borrador); sin eso se usan las plantillas.
"""

from __future__ import annotations

import html
import logging
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.testclient import TestClient

from copiloto.app import create_app
from copiloto.config import Settings
from copiloto.meli.client import MeliClient
from copiloto.meli.fake import DEFAULT_SELLER_ID, FakeMeliState, create_fake_app, seed_default_scenarios
from copiloto.meli.oauth import TokenProvider
from copiloto.store import Store
from copiloto.worker import Worker

logger = logging.getLogger(__name__)

BUYER_ACTIONS: dict[str, str] = {
    "aceptar": "Comprador acepta la oferta",
    "rechazar": "Comprador rechaza y pide reembolso completo",
    "escalar": "Comprador pide mediación",
    "favor_vendedor": "ML falla a favor del vendedor",
    "favor_comprador": "ML falla a favor del comprador",
}


@dataclass
class Sandbox:
    app: FastAPI
    worker: Worker
    state: FakeMeliState
    store: Store
    settings: Settings
    _stop: threading.Event = field(default_factory=threading.Event)

    def notify(self, claim_id: str) -> None:
        """Lo mismo que hace el webhook al recibir una notificación de ML."""
        payload = self.state.notification_payload(claim_id)
        dedupe_key = f"{payload['topic']}|{payload['resource']}|{payload['sent']}|{uuid.uuid4().hex[:6]}"
        is_new = self.store.save_notification(
            dedupe_key,
            payload["topic"],
            payload["resource"],
            claim_id,
            str(payload["user_id"]),
            payload["application_id"],
        )
        if is_new:
            self.store.enqueue_job("process_claim", {"seller_id": str(payload["user_id"]), "claim_id": claim_id})

    def new_claim(self, template_id: str) -> str:
        numeric = [int(c) for c in self.state.claims if c.isdigit()]
        claim_id = str(max(numeric, default=9000) + 1)
        self.state.new_claim(claim_id, **self.state.templates[template_id])
        self.notify(claim_id)
        return claim_id

    def buyer_action(self, claim_id: str, action: str) -> None:
        s = self.state
        if action == "aceptar":
            s.buyer_accepts_offer(claim_id)
        elif action == "rechazar":
            s.buyer_rejects_offer(claim_id)
        elif action == "escalar":
            s.buyer_escalates(claim_id)
        elif action == "favor_vendedor":
            s.ml_resolves(claim_id, "respondent")
        elif action == "favor_comprador":
            s.ml_resolves(claim_id, "complainant")
        else:
            raise ValueError(f"acción desconocida: {action}")
        self.notify(claim_id)

    def start_worker(self, poll_seconds: float = 0.5) -> threading.Thread:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    if not self.worker.run_once():
                        self._stop.wait(poll_seconds)
                except Exception:  # el sandbox no debe morir por un job roto
                    logger.exception("job falló en el sandbox")
                    self._stop.wait(1.0)

        thread = threading.Thread(target=loop, name="copiloto-sandbox-worker", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def build_sandbox(settings: Settings, llm_client: Any = None, db_path: str | None = None) -> Sandbox:
    db_path = db_path or f"{tempfile.mkdtemp(prefix='copiloto-sandbox-')}/copiloto-sandbox.db"
    sb_settings = settings.model_copy(
        update={
            "db_path": db_path,
            "mode": "approve",  # aprobar en el dashboard ejecuta contra la API SIMULADA
            "llm_enabled": llm_client is not None,
            "app_id": "APPSANDBOX",
        }
    )
    state = FakeMeliState(app_id=sb_settings.app_id)
    seed_default_scenarios(state)
    fake_app, state = create_fake_app(state)
    fake_client = TestClient(fake_app)

    store = Store(sb_settings.db_path, sb_settings.secret_key)
    access_token, refresh_token = state.issue_tokens(DEFAULT_SELLER_ID)
    store.upsert_seller(
        DEFAULT_SELLER_ID, state.seller_nickname, access_token, refresh_token, datetime.now(UTC) + timedelta(hours=3)
    )
    token_provider = TokenProvider(store, sb_settings, http_client=fake_client)
    meli = MeliClient(sb_settings.meli_base_url, token_provider, http_client=fake_client, sleep_fn=lambda _s: None)
    worker = Worker(store, sb_settings, meli, llm_client=llm_client)
    app = create_app(sb_settings, store=store, http_client=fake_client)
    sandbox = Sandbox(app=app, worker=worker, state=state, store=store, settings=sb_settings)
    _mount_routes(sandbox)
    for claim_id in sorted(state.claims):
        sandbox.notify(claim_id)
    return sandbox


def _mount_routes(sb: Sandbox) -> None:
    app = sb.app

    @app.get("/sandbox", response_class=HTMLResponse)
    def sandbox_home() -> HTMLResponse:
        return HTMLResponse(_render(sb))

    @app.post("/sandbox/nuevo")
    def sandbox_new(plantilla: str = Form(...)) -> RedirectResponse:  # noqa: B008 (patrón estándar de FastAPI)
        if plantilla in sb.state.templates:
            sb.new_claim(plantilla)
        return RedirectResponse("/sandbox", status_code=303)

    @app.post("/sandbox/evento/{claim_id}")
    def sandbox_event(claim_id: str, accion: str = Form(...)) -> RedirectResponse:  # noqa: B008
        if claim_id in sb.state.claims and accion in BUYER_ACTIONS:
            sb.buyer_action(claim_id, accion)
        return RedirectResponse("/sandbox", status_code=303)


def _render(sb: Sandbox) -> str:
    e = html.escape
    options = "".join(
        f'<option value="{e(cid)}">{e(cid)} · {e(str(kw.get("reason_name")))} '
        f"(${kw.get('order_amount', 0):,.0f})</option>"
        for cid, kw in sorted(sb.state.templates.items())
        if cid.isdigit() and int(cid) < 9000
    )
    rows = []
    for cid in sorted(sb.state.claims, key=lambda c: int(c) if c.isdigit() else 0, reverse=True):
        claim = sb.state.claims[cid]
        stored = sb.store.get_claim_row(cid) or {}
        rec = sb.store.get_latest_recommendation(cid) or {}
        msgs = [m for m in sb.state.messages.get(cid, []) if m.get("sender_role") == "respondent"]
        last_seller = msgs[-1]["message"] if msgs else "—"
        offer = claim.get("pending_partial_offer")
        buttons = ""
        if claim["status"] == "opened":
            buttons = "".join(
                f'<button name="accion" value="{k}">{e(label)}</button>' for k, label in BUYER_ACTIONS.items()
            )
            buttons = f'<form method="post" action="/sandbox/evento/{e(cid)}">{buttons}</form>'
        rows.append(
            "<tr>"
            f'<td><a href="/claims/{e(cid)}">{e(cid)}</a></td>'
            f"<td>{e(claim['status'])} / {e(claim['stage'])}</td>"
            f"<td>{e(str(stored.get('category') or '…'))}</td>"
            f"<td>{e(str(rec.get('action') or '…'))}</td>"
            f"<td>{e(f'{offer}%') if offer else '—'}</td>"
            f"<td class=msg>{e(last_seller)}</td>"
            f"<td>{buttons}</td>"
            "</tr>"
        )
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5"><title>Sandbox del copiloto</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid #ddd;padding:6px;
text-align:left;vertical-align:top}}.msg{{max-width:320px;color:#444}}button{{margin:2px;font-size:12px}}
.box{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:16px}}
@media (prefers-color-scheme: dark){{body{{background:#141414;color:#eee}}.box{{background:#1e1e1e;border-color:#333}}
td,th{{border-color:#333}}.msg{{color:#bbb}}a{{color:#8ab4ff}}}}
</style></head><body>
<h1>Sandbox — API de Mercado Libre simulada</h1>
<div class="box">Tú haces de comprador y de Mercado Libre; el copiloto hace de vendedor.
Aprueba o rechaza en el <a href="/">panel del copiloto</a> (ahí sí es el dashboard real).
Esta página se refresca sola cada 5 s.</div>
<div class="box"><form method="post" action="/sandbox/nuevo">Abrir reclamo nuevo como:
<select name="plantilla">{options}</select> <button>Abrir</button></form></div>
<table><tr><th>Reclamo</th><th>Estado</th><th>Categoría</th><th>Recomendación</th><th>Oferta</th>
<th>Último mensaje del vendedor</th><th>Tú (comprador / ML)</th></tr>{"".join(rows)}</table>
</body></html>"""

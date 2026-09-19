"""test_dashboard.py — dashboard Jinja2: lista, detalle, aprobar/rechazar y el basic auth
opcional. La prueba de auth es una prueba de regresión: `Annotated[X, Depends(security)]` con
`security` como variable local de `create_app` se rompe en silencio bajo `from __future__
import annotations` (FastAPI no puede resolver el `Depends` desde el string de la anotación
porque solo tiene los globals del módulo, no los locals de la función envolvente) — el
parámetro se reinterpreta como query param y la auth queda "siempre 401" o "siempre pasa"
según el caso, sin ningún error visible. `app.py` usa `= Depends(security)` como valor por
defecto (estilo viejo) específicamente para evitar esto; este test evita que alguien lo
"simplifique" de vuelta a `Annotated` sin darse cuenta de por qué se rompe."""

from __future__ import annotations

from fastapi.testclient import TestClient

from copiloto import pipeline
from copiloto.app import create_app


def test_dashboard_open_without_credentials_by_default(app_client):
    resp = app_client.get("/")
    assert resp.status_code == 200


def test_dashboard_requires_basic_auth_when_configured(settings, store, fake):
    fake_client, _state = fake
    protected = settings.model_copy(update={"dashboard_user": "admin", "dashboard_password": "secret"})
    app = create_app(protected, store=store, http_client=fake_client)
    client = TestClient(app)

    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "wrong-password")).status_code == 401
    assert client.get("/", auth=("admin", "secret")).status_code == 200
    assert client.get("/api/claims", auth=("admin", "secret")).status_code == 200


def test_list_shows_open_claims_with_recommendation(app_client, store, settings, meli, seller_registered):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "1001" in resp.text
    assert "defend" in resp.text


def test_detail_shows_ranking_and_draft(app_client, store, settings, meli, seller_registered):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    resp = app_client.get("/claims/1001")
    assert resp.status_code == 200
    assert "Ranking de acciones" in resp.text
    assert "Borrador de respuesta" in resp.text


def test_detail_404_for_unknown_claim(app_client):
    assert app_client.get("/claims/no-existe").status_code == 404


def test_approve_with_violating_message_does_not_approve(app_client, store, settings, meli, seller_registered):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")

    resp = app_client.post("/claims/1001/approve", data={"message": "Escríbeme a hola@tienda.com."})

    assert resp.status_code == 400
    assert "no se puede enviar" in resp.text
    assert store.job_counts().get("queued", 0) == 0  # no se encoló ningún `execute`


def test_approve_with_clean_message_redirects_and_enqueues_execution(
    app_client, store, settings, meli, seller_registered
):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    draft = store.get_latest_draft("1001")

    resp = app_client.post("/claims/1001/approve", data={"message": draft["message"]}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/claims/1001"
    assert store.job_counts().get("queued", 0) == 1
    approved_events = [e for e in store.list_events(claim_id="1001") if e["kind"] == "approved"]
    assert len(approved_events) == 1


def test_reject_records_decision(app_client, store, settings, meli, seller_registered):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")

    resp = app_client.post("/claims/1001/reject", follow_redirects=False)

    assert resp.status_code == 303
    rejected_events = [e for e in store.list_events(claim_id="1001") if e["kind"] == "rejected"]
    assert len(rejected_events) == 1
    assert store.job_counts().get("queued", 0) == 0  # rechazar no ejecuta nada

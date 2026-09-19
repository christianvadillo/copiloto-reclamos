"""test_webhook.py — el webhook responde rápido, deduplica, valida `application_id` e
ignora tópicos que no son de reclamos, sin llamar nunca a la API de Mercado Libre."""

from __future__ import annotations


def test_responds_200_and_ok_for_claim_topic(app_client, fake):
    _fake_client, state = fake
    resp = app_client.post("/notifications", json=state.notification_payload("1001"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_enqueues_process_claim_job(app_client, fake, store):
    _fake_client, state = fake
    app_client.post("/notifications", json=state.notification_payload("1001"))
    assert store.job_counts() == {"queued": 1}
    job = store.claim_job()
    assert job.kind == "process_claim"
    assert job.payload == {"seller_id": state.seller_id, "claim_id": "1001"}


def test_dedupes_by_topic_resource_sent(app_client, fake, store):
    _fake_client, state = fake
    payload = state.notification_payload("1001")
    first = app_client.post("/notifications", json=payload)
    second = app_client.post("/notifications", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert store.job_counts() == {"queued": 1}  # la segunda no volvió a encolar


def test_different_sent_timestamps_are_not_duplicates(app_client, fake, store):
    _fake_client, state = fake
    p1 = state.notification_payload("1001")
    p2 = dict(p1, sent="2026-01-01T00:00:00+00:00", _id="otro-id")
    app_client.post("/notifications", json=p1)
    app_client.post("/notifications", json=p2)
    assert store.job_counts() == {"queued": 2}


def test_rejects_foreign_application_id(app_client, fake, store):
    _fake_client, state = fake
    payload = state.notification_payload("1001")
    payload["application_id"] = "UNA_APP_AJENA"
    resp = app_client.post("/notifications", json=payload)
    assert resp.status_code == 403
    assert store.job_counts() == {}


def test_ignores_topics_that_are_not_claims(app_client, fake, store):
    _fake_client, state = fake
    payload = state.notification_payload("1001", topic="orders_v2")
    resp = app_client.post("/notifications", json=payload)
    assert resp.status_code == 200
    assert store.job_counts() == {}


def test_malformed_body_still_acks_fast(app_client):
    # Un payload que no es ni siquiera un dict no debe tumbar el webhook (ML espera 200 rápido).
    resp = app_client.post("/notifications", json=[1, 2, 3])
    assert resp.status_code in (200, 422)  # FastAPI valida el tipo del body; nunca 5xx


def test_health_endpoint(app_client, settings):
    resp = app_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "mode": settings.mode}

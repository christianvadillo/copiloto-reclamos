"""test_pipeline_e2e.py — `process_claim` contra la API falsa, para cada uno de los 7
escenarios sembrados (ESPECIFICACION.md §9): categoría correcta, hay recomendación, y el
borrador que se guarda pasa sus propios guardrails."""

from __future__ import annotations

import pytest

from copiloto import pipeline
from copiloto.domain import Action, Category
from copiloto.drafting.guardrails import check_message

EXPECTED_CATEGORY = {
    "1001": Category.NO_RECIBIDO,  # PNR entregado, evidencia fuerte
    "1002": Category.NO_RECIBIDO,  # PNR en tránsito
    "2001": Category.DEFECTUOSO,  # PDD2
    "2002": Category.DIFERENTE,
    "2003": Category.DEVOLUCION,  # PDD9939, arrepentimiento
    "3001": Category.INCOMPLETO,
    "4001": Category.CANCELACION,
}


@pytest.mark.parametrize("claim_id", sorted(EXPECTED_CATEGORY))
def test_process_claim_classifies_recommends_and_drafts(claim_id, store, settings, meli, seller_registered):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id=claim_id)

    row = store.get_claim_row(claim_id)
    assert row is not None
    assert row["category"] == EXPECTED_CATEGORY[claim_id].value
    assert row["seller_id"] == seller_registered
    assert row["snapshot_hash"]

    rec = store.get_latest_recommendation(claim_id)
    assert rec is not None
    assert rec["action"] in {a.value for a in Action}
    assert rec["expected_cost"] >= 0
    assert 0.0 <= rec["prob_best"] <= 1.0

    draft = store.get_latest_draft(claim_id)
    assert draft is not None
    assert draft["source"] == "template"  # llm_enabled=False en la fixture `settings`
    action = Action(rec["action"])
    violations = check_message(draft["message"], action, rec["params"] or {}, settings.message_max_chars)
    assert violations == [], f"el borrador de {claim_id} violó guardrails: {violations}"

    # Auditoría: cada paso deja rastro.
    kinds = {e["kind"] for e in store.list_events(claim_id=claim_id)}
    assert {"classified", "recommended", "drafted"} <= kinds


def test_pnr_delivered_strong_evidence_defends(store, settings, meli, seller_registered):
    """Coincide con `test_decision.test_pnr_delivered_with_strong_evidence_defends`: entregado
    por Mercado Envíos, antes de abrirse el reclamo → el copiloto defiende la venta."""
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    rec = store.get_latest_recommendation("1001")
    assert rec["action"] == "defend"


def test_pnr_in_transit_informs_tracking(store, settings, meli, seller_registered):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1002")
    rec = store.get_latest_recommendation("1002")
    assert rec["action"] == "inform_tracking"


def test_process_claim_is_idempotent_on_unchanged_snapshot(store, settings, meli, seller_registered):
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    first = store.get_latest_recommendation("1001")
    first_draft = store.get_latest_draft("1001")

    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    second = store.get_latest_recommendation("1001")
    second_draft = store.get_latest_draft("1001")

    assert first["id"] == second["id"]  # no se generó una recomendación nueva
    assert first_draft["id"] == second_draft["id"]


def test_process_claim_reprocesses_after_a_new_buyer_message(store, settings, meli, seller_registered, fake):
    _fake_client, state = fake
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    first = store.get_latest_recommendation("1001")

    state.messages["1001"].append(
        {"sender_role": "complainant", "message": "¿ya revisaron mi caso?", "date": "2026-01-01T00:00:00+00:00"}
    )
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    second = store.get_latest_recommendation("1001")

    assert second["id"] != first["id"]  # el snapshot cambió: sí genera una recomendación nueva


def test_auto_mode_auto_approves_and_enqueues_execution(store, settings, meli, seller_registered):
    from copiloto.decision.recommender import Policy

    auto_settings = settings.model_copy(update={"mode": "auto", "auto_max_amount": 10_000.0, "min_prob_best": 0.0})
    assert auto_settings.mode == "auto"
    # Reclamo 1002 (PNR en tránsito) recomienda `inform_tracking`, que sí está en
    # `Policy().auto_actions` por defecto.
    assert Policy().auto_actions
    pipeline.process_claim(store=store, settings=auto_settings, meli=meli, seller_id=seller_registered, claim_id="1002")
    rec = store.get_latest_recommendation("1002")
    assert rec["action"] == "inform_tracking"
    assert rec["requires_approval"] is False

    kinds = [e["kind"] for e in store.list_events(claim_id="1002")]
    assert "auto_approved" in kinds
    jobs = store.job_counts()
    assert jobs.get("queued", 0) >= 1

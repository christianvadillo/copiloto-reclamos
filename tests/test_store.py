"""test_store.py — migraciones idempotentes y que un reclamo cerrado quede como `Outcome`
que el `PriorBook` puede contar (el ciclo de aprendizaje completo, no solo el guardado)."""

from __future__ import annotations

from cryptography.fernet import Fernet

from copiloto import actions, pipeline
from copiloto.decision.priors import PriorBook, counts_from_outcomes
from copiloto.store import Store


def test_migrations_are_idempotent(tmp_path):
    db_path = str(tmp_path / "idempotent.db")
    key = Fernet.generate_key()

    store = Store(db_path, key)
    store.migrate()  # correrlas de más no debe fallar ni duplicar nada
    store.migrate()
    store.close()

    reopened = Store(db_path, key)  # el __init__ vuelve a migrar sobre una base ya migrada
    assert reopened.get_claim_row("no-existe") is None
    reopened.close()


def test_outcomes_for_seller_empty_by_default(store, seller_registered):
    assert store.outcomes_for_seller(seller_registered) == []


def test_closed_claim_outcome_appears_in_priorbook_counts(store, settings, meli, seller_registered, fake):
    _fake_client, _state = fake
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")

    approval_id = store.create_approval(
        claim_id="1001",
        seller_id=seller_registered,
        recommendation_id=None,
        draft_id=None,
        action="refund_full",
        params={},
        decision="approved",
        edited_message="Procesamos tu reembolso total. — Equipo",
        approved_by="test",
    )
    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=approval_id)

    # El reclamo ya cerró en la API falsa (refund cierra con benefited=complainant): al
    # reprocesar, el pipeline lo detecta y registra el Outcome.
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")

    outcomes = store.outcomes_for_seller(seller_registered)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.category.value == "no_recibido"
    assert outcome.action.value == "refund_full"  # lo que se EJECUTÓ, no lo recomendado

    book = PriorBook(counts_from_outcomes(outcomes))
    assert book.n_obs(("esc", "no_recibido", "refund_full")) == 1.0

    # Reprocesar de nuevo no duplica el outcome (upsert por claim_id).
    pipeline.process_claim(store=store, settings=settings, meli=meli, seller_id=seller_registered, claim_id="1001")
    assert len(store.outcomes_for_seller(seller_registered)) == 1


def test_notification_to_draft_latency_is_recorded(app_client, worker, fake, store):
    _fake_client, state = fake
    app_client.post("/notifications", json=state.notification_payload("1001"))
    worker.run_until_idle()
    latencies = store.notification_to_draft_latencies_seconds()
    assert len(latencies) == 1
    assert latencies[0] >= 0.0

"""test_actions.py — ejecutar una aprobación mueve el estado en la API falsa, es idempotente
(doble aprobación ejecuta una sola vez) y en modo sombra nunca llama a Mercado Libre."""

from __future__ import annotations

from copiloto import actions
from copiloto.meli.client import MeliError


def _create_approval(store, claim_id, seller_id, action, params, message="Hola, gracias por tu paciencia. — Equipo"):
    return store.create_approval(
        claim_id=claim_id,
        seller_id=seller_id,
        recommendation_id=None,
        draft_id=None,
        action=action,
        params=params,
        decision="approved",
        edited_message=message,
        approved_by="test",
    )


def test_refund_execution_closes_claim_in_fake_api(store, settings, meli, seller_registered, fake):
    _fake_client, state = fake
    messages_before = len(state.messages["1001"])  # el escenario ya trae un mensaje del comprador
    approval_id = _create_approval(store, "1001", seller_registered, "refund_full", {})

    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=approval_id)

    assert state.claims["1001"]["status"] == "closed"
    assert state.claims["1001"]["resolution"]["benefited"] == ["complainant"]
    assert store.execution_exists("1001:refund_full")
    assert len(state.messages["1001"]) == messages_before + 1  # el mensaje siempre va primero


def test_partial_refund_execution_registers_pending_offer_without_closing(
    store, settings, meli, seller_registered, fake
):
    _fake_client, state = fake
    approval_id = _create_approval(store, "2001", seller_registered, "partial_refund", {"pct": 0.2})

    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=approval_id)

    assert state.claims["2001"]["pending_partial_offer"] == 20
    assert state.claims["2001"]["status"] == "opened"  # "partial queda pendiente" (spec §9)


def test_double_approval_executes_only_once(store, settings, meli, seller_registered, fake):
    _fake_client, state = fake
    messages_before = len(state.messages["1001"])
    approval_1 = _create_approval(store, "1001", seller_registered, "refund_full", {})
    approval_2 = _create_approval(store, "1001", seller_registered, "refund_full", {})

    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=approval_1)
    messages_after_first = len(state.messages["1001"])

    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=approval_2)
    messages_after_second = len(state.messages["1001"])

    assert messages_after_first == messages_after_second == messages_before + 1
    executed_events = [e for e in store.list_events(claim_id="1001") if e["kind"] == "executed"]
    assert len(executed_events) == 1
    skipped = [e for e in store.list_events(claim_id="1001") if e["kind"] == "execution_skipped_duplicate"]
    assert len(skipped) == 1


def test_shadow_mode_never_executes(store, settings, meli, seller_registered, fake):
    _fake_client, state = fake
    messages_before = len(state.messages["1001"])
    shadow_settings = settings.model_copy(update={"mode": "shadow"})
    approval_id = _create_approval(store, "1001", seller_registered, "refund_full", {})

    actions.execute_approval(store=store, settings=shadow_settings, meli=meli, approval_id=approval_id)

    assert state.claims["1001"]["status"] == "opened"
    assert len(state.messages["1001"]) == messages_before  # no se mandó nada: modo sombra
    assert not store.execution_exists("1001:refund_full")
    skipped = [e for e in store.list_events(claim_id="1001") if e["kind"] == "execution_skipped_shadow"]
    assert len(skipped) == 1


def test_execution_aborts_if_claim_closed_meanwhile(store, settings, meli, seller_registered, fake):
    _fake_client, state = fake
    state.claims["1001"]["status"] = "closed"  # alguien más ya lo resolvió
    approval_id = _create_approval(store, "1001", seller_registered, "refund_full", {})

    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=approval_id)

    assert store.get_latest_execution("1001")["status"] == "aborted"
    aborted = [e for e in store.list_events(claim_id="1001") if e["kind"] == "execution_aborted_closed"]
    assert len(aborted) == 1


def test_execution_aborts_if_action_no_longer_available(store, settings, meli, seller_registered, fake):
    _fake_client, state = fake
    # Nos quedamos solo con la acción de mensaje: refund ya no está disponible.
    for player in state.claims["2001"]["players"]:
        if player["role"] == "respondent":
            player["available_actions"] = [a for a in player["available_actions"] if a["action"] != "refund"]
    approval_id = _create_approval(store, "2001", seller_registered, "refund_full", {})

    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=approval_id)

    assert store.get_latest_execution("2001")["status"] == "aborted"
    aborted = [e for e in store.list_events(claim_id="2001") if e["kind"] == "execution_aborted_unavailable"]
    assert len(aborted) == 1


def test_defend_uploads_evidence_photos(tmp_path, store, settings, meli, seller_registered, fake):
    _fake_client, state = fake
    order_id = state.claims["2001"]["related_entities"][0]["id"]
    photos_dir = tmp_path / order_id
    photos_dir.mkdir(parents=True)
    (photos_dir / "foto1.jpg").write_bytes(b"contenido-fake-de-foto")
    settings_with_photos = settings.model_copy(update={"evidence_photos_dir": str(tmp_path)})
    approval_id = _create_approval(
        store, "2001", seller_registered, "defend", {}, message="Defendemos la venta con evidencia. — Equipo"
    )

    actions.execute_approval(store=store, settings=settings_with_photos, meli=meli, approval_id=approval_id)

    assert store.execution_exists("2001:defend")
    assert len(state.attachments.get("2001", [])) == 1
    assert state.attachments["2001"][0]["filename"] == "foto1.jpg"


# ── Idempotencia por pasos: nunca repetir a ciegas algo que mueve dinero ────────────────────


def _messages_to_buyer(state, claim_id):
    return [m for m in state.messages.get(claim_id, []) if m.get("sender_role") == "respondent"]


def test_failed_step_resumes_without_resending_message(store, settings, meli, seller_registered, fake, monkeypatch):
    _fake_client, state = fake
    before = len(_messages_to_buyer(state, "1001"))
    original_refund = meli.refund
    calls = {"n": 0}

    def flaky_refund(seller_id, claim_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise MeliError(400, "claim_locked")  # respuesta clara: ML no aplicó nada
        return original_refund(seller_id, claim_id)

    monkeypatch.setattr(meli, "refund", flaky_refund)
    first = _create_approval(store, "1001", seller_registered, "refund_full", {})
    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=first)
    assert store.get_latest_execution("1001")["status"] == "failed"
    assert len(_messages_to_buyer(state, "1001")) == before + 1

    second = _create_approval(store, "1001", seller_registered, "refund_full", {})
    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=second)
    assert store.get_latest_execution("1001")["status"] == "done"
    assert len(_messages_to_buyer(state, "1001")) == before + 1  # el mensaje NO se reenvió
    assert calls["n"] == 2


def test_uncertain_post_is_never_repeated(store, settings, meli, seller_registered, fake, monkeypatch):
    calls = {"n": 0}

    def timeout_refund(seller_id, claim_id):
        calls["n"] += 1
        raise MeliError(0, "read timeout", uncertain=True)

    monkeypatch.setattr(meli, "refund", timeout_refund)
    first = _create_approval(store, "1001", seller_registered, "refund_full", {})
    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=first)
    second = _create_approval(store, "1001", seller_registered, "refund_full", {})
    actions.execute_approval(store=store, settings=settings, meli=meli, approval_id=second)
    assert calls["n"] == 1
    kinds = [e["kind"] for e in store.list_events(claim_id="1001")]
    assert kinds.count("execution_in_doubt") == 2

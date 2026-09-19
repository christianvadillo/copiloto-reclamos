"""Formatos reales de la API de ML observados en vivo (reclamo 5579999933, 19-sep-2026)."""

from __future__ import annotations

from copiloto.pipeline import _expected_actions, _logistic_type


def test_expected_resolutions_real_list_format():
    body = [
        {
            "player_role": "complainant",
            "user_id": 3699950922,
            "expected_resolution": "return_product",
            "status": "pending",
        }
    ]
    assert _expected_actions(body) == frozenset({"return_product"})


def test_expected_resolutions_legacy_format_and_empty():
    assert _expected_actions({"expected_resolutions": [{"action": "refund"}]}) == frozenset({"refund"})
    assert _expected_actions([]) == frozenset()
    assert _expected_actions(None) == frozenset()


def test_logistic_type_nested_and_flat():
    assert _logistic_type({"logistic": {"mode": "me2", "type": "fulfillment", "direction": "forward"}}) == "fulfillment"
    assert _logistic_type({"logistic": {"mode": "custom", "type": None, "direction": "forward"}}) is None
    assert _logistic_type({"logistic_type": "drop_off"}) == "drop_off"
    assert _logistic_type({}) is None


def test_order_id_from_resource_fields():
    from copiloto.pipeline import extract_order_id

    real = {"resource": "order", "resource_id": 2000018545709136, "related_entities": []}
    assert extract_order_id(real) == "2000018545709136"
    assert extract_order_id({"related_entities": [{"type": "order", "id": 7}]}) == "7"
    assert extract_order_id({"resource": "shipment", "resource_id": 9}) is None

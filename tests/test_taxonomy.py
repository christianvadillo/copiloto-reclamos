import pytest

from copiloto.domain import Action, Category
from copiloto.taxonomy import classify, execution_plan, map_available_actions


@pytest.mark.parametrize(
    ("reason_id", "ctype", "name", "buyer", "expected"),
    [
        ("PNR3430", "mediations", None, None, Category.NO_RECIBIDO),
        ("CS1", "mediations", None, None, Category.CANCELACION),
        (None, "cancel_purchase", None, None, Category.CANCELACION),
        ("PDD2", "mediations", "El paquete llegó dañado", None, Category.DEFECTUOSO),
        ("PDD9939", "mediations", "Llegó en buenas condiciones pero no lo quiero", None, Category.DEVOLUCION),
        ("PDD100", "mediations", None, "me llegó otro modelo, no es el que pedí", Category.DIFERENTE),
        ("PDD101", "mediations", None, "le faltan piezas", Category.INCOMPLETO),
        ("PDD102", "mediations", None, "hola", Category.DEFECTUOSO),
        (None, "service", None, None, Category.OTRO),
    ],
)
def test_classify(reason_id, ctype, name, buyer, expected):
    assert classify(reason_id, ctype, name, None, buyer).category is expected


def test_low_confidence_flags_second_opinion():
    assert classify("PDD102", "mediations", None, None, "hola").needs_second_opinion
    assert not classify("PNR1", "mediations").needs_second_opinion


def test_map_available_actions():
    acts = map_available_actions(["refund", "allow_return", "allow_partial_refund", "send_message_to_complainant"])
    assert {Action.REFUND_FULL, Action.RETURN_REFUND, Action.PARTIAL_REFUND, Action.DEFEND} <= acts
    dispute = map_available_actions(["send_message_to_mediator", "send_message_to_complainant"], stage="dispute")
    assert dispute == frozenset({Action.DEFEND})


def test_execution_plan_partial_refund():
    calls = execution_plan(Action.PARTIAL_REFUND, pct=0.2)
    assert calls[-1].path.endswith("/expected-resolutions/partial-refund")
    assert calls[-1].body == {"percentage": 20}
    with pytest.raises(ValueError):
        execution_plan(Action.PARTIAL_REFUND, pct=1.0)


def test_execution_plan_dispute_goes_to_mediator():
    calls = execution_plan(Action.DEFEND, stage="dispute")
    assert calls == [calls[0]] and calls[0].body == {"receiver_role": "mediator"}

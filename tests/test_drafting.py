"""test_drafting.py — plantillas dentro del límite para toda combinación, guardrails caso por
caso, y el orquestador LLM → guardrails → un reintento → plantilla."""

from __future__ import annotations

import pytest

from copiloto.config import Settings
from copiloto.domain import Action, Category
from copiloto.drafting import drafter, templates
from copiloto.drafting.guardrails import check_message
from copiloto.drafting.llm import Borrador

MAX_CHARS = 350


# ── plantillas ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("category", list(Category))
@pytest.mark.parametrize("action", list(Action))
def test_template_fits_limit_and_passes_guardrails_for_every_combination(category, action):
    params = {"amount": 246.0, "pct": 0.2}
    text = templates.render_template(
        category,
        action,
        params=params,
        store_name="Mi Tienda",
        signature="El equipo de Mi Tienda",
        tracking_number="1234567890123",
        eta="25 de septiembre",
    )
    assert len(text) <= MAX_CHARS
    assert check_message(text, action, params, MAX_CHARS) == []


def test_template_never_empty():
    for category in Category:
        for action in Action:
            text = templates.render_template(category, action, store_name="Tienda", signature="Equipo")
            assert text.strip()


# ── guardrails ──────────────────────────────────────────────────────────────────────────────


def test_guardrail_catches_email():
    violations = check_message("Escríbeme a vendedor@tienda.com por favor.", Action.DEFEND, {}, MAX_CHARS)
    assert any("correo" in r for r in violations)


def test_guardrail_catches_phone():
    violations = check_message("Llámame al 5512345678 porfa.", Action.DEFEND, {}, MAX_CHARS)
    assert any("telefónico" in r for r in violations)


def test_guardrail_does_not_flag_long_tracking_numbers_as_phones():
    violations = check_message("Tu guía es 1234567890123, llega pronto.", Action.DEFEND, {}, MAX_CHARS)
    assert violations == []


def test_guardrail_catches_url():
    violations = check_message("Visita www.tienda.com para más info.", Action.DEFEND, {}, MAX_CHARS)
    assert any("URL" in r for r in violations)


def test_guardrail_catches_off_platform_channel():
    violations = check_message("Mejor hablamos por WhatsApp.", Action.DEFEND, {}, MAX_CHARS)
    assert any("fuera de Mercado Libre" in r for r in violations)


def test_guardrail_catches_mediation_threat():
    violations = check_message("Si no aceptas, abriremos una mediación.", Action.DEFEND, {}, MAX_CHARS)
    assert any("amenaza" in r for r in violations)


def test_guardrail_requires_amount_or_percent_for_partial_refund():
    violations = check_message("Te ofrecemos un descuento razonable.", Action.PARTIAL_REFUND, {}, MAX_CHARS)
    assert any("monto exacto" in r for r in violations)
    assert check_message("Te ofrecemos $200 de descuento.", Action.PARTIAL_REFUND, {}, MAX_CHARS) == []
    assert check_message("Te ofrecemos 20% de descuento.", Action.PARTIAL_REFUND, {}, MAX_CHARS) == []


def test_guardrail_forbids_defend_promising_refund():
    violations = check_message("De todas formas te hacemos el reembolso.", Action.DEFEND, {}, MAX_CHARS)
    assert any("reembolso o devolución" in r for r in violations)


def test_guardrail_clean_message_passes():
    text = "Hola, gracias por tu mensaje. Ya estamos revisando tu caso. — El equipo"
    assert check_message(text, Action.DEFEND, {}, MAX_CHARS) == []


def test_guardrail_length():
    violations = check_message("x" * 400, Action.DEFEND, {}, MAX_CHARS)
    assert any("caracteres" in r for r in violations)


def test_guardrail_empty_message():
    assert check_message("   ", Action.DEFEND, {}, MAX_CHARS) != []


# ── drafter: LLM → guardrails → un reintento → plantilla ──────────────────────────────────


class _FakeMessages:
    def __init__(self, responses=None, error=None):
        self._responses = list(responses or [])
        self.error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self._responses.pop(0)


class _FakeBeta:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


class FakeAnthropicClient:
    """Doble de prueba de `anthropic.Anthropic`: expone `.beta.messages.parse(...)` con
    respuestas o un error preconfigurados. Nunca toca la red."""

    def __init__(self, responses=None, error: Exception | None = None) -> None:
        self.beta = _FakeBeta(_FakeMessages(responses=responses, error=error))


class _Resp:
    def __init__(self, parsed_output, stop_reason: str = "end_turn") -> None:
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason


def _settings(**overrides) -> Settings:
    base = {"llm_enabled": True, "message_max_chars": MAX_CHARS, "store_name": "Mi Tienda", "signature": "El equipo"}
    base.update(overrides)
    return Settings(**base)


def _draft(client, settings: Settings | None = None):
    return drafter.draft(
        category=Category.DEFECTUOSO,
        action=Action.REFUND_FULL,
        params={},
        evidence_summary="estado del envío: entregado",
        buyer_messages=["el producto llegó roto"],
        settings=settings or _settings(),
        llm_client=client,
    )


def test_drafter_uses_llm_when_clean():
    clean = Borrador(
        mensaje="Hola, procesamos tu reembolso total. — El equipo", resumen_vendedor="Reembolso total.", riesgos=[]
    )
    client = FakeAnthropicClient(responses=[_Resp(clean)])

    result = _draft(client)

    assert result.source == "llm"
    assert result.message == clean.mensaje
    assert result.violations == ()
    assert len(client.beta.messages.calls) == 1


def test_drafter_retries_once_then_succeeds():
    dirty = Borrador(mensaje="Escríbeme a hola@tienda.com y seguimos por ahí.", resumen_vendedor="x", riesgos=[])
    clean = Borrador(mensaje="Hola, procesamos tu reembolso total. — El equipo", resumen_vendedor="x", riesgos=[])
    client = FakeAnthropicClient(responses=[_Resp(dirty), _Resp(clean)])

    result = _draft(client)

    assert result.source == "llm"
    assert result.message == clean.mensaje
    assert len(client.beta.messages.calls) == 2


def test_drafter_falls_back_to_template_when_llm_keeps_violating():
    dirty = Borrador(mensaje="Escríbeme a hola@tienda.com.", resumen_vendedor="x", riesgos=[])
    client = FakeAnthropicClient(responses=[_Resp(dirty), _Resp(dirty)])

    result = _draft(client)

    assert result.source == "template"
    assert check_message(result.message, Action.REFUND_FULL, {}, MAX_CHARS) == []
    assert len(client.beta.messages.calls) == 2  # sí reintentó una vez antes de rendirse


def test_drafter_falls_back_to_template_when_llm_raises_generic_error():
    client = FakeAnthropicClient(error=RuntimeError("el cliente falso truena a propósito"))

    result = _draft(client)

    assert result.source == "template"


def test_drafter_falls_back_to_template_when_llm_refuses():
    client = FakeAnthropicClient(responses=[_Resp(None, stop_reason="refusal")])

    result = _draft(client)

    assert result.source == "template"


def test_drafter_uses_template_when_llm_disabled():
    client = FakeAnthropicClient()

    result = _draft(client, settings=_settings(llm_enabled=False))

    assert result.source == "template"
    assert client.beta.messages.calls == []  # nunca se llamó al cliente


def test_drafter_uses_template_when_no_llm_client_given():
    result = drafter.draft(
        category=Category.DEFECTUOSO,
        action=Action.REFUND_FULL,
        params={},
        evidence_summary="",
        buyer_messages=[],
        settings=_settings(),
        llm_client=None,
    )
    assert result.source == "template"


def test_partial_refund_must_quote_the_executed_percentage():
    params = {"pct": 0.2, "amount": 240.0}
    ok = check_message(
        "Te ofrecemos un reembolso del 20% ($240.00) y te quedas el producto.", Action.PARTIAL_REFUND, params, MAX_CHARS
    )
    assert ok == []
    wrong_pct = check_message(
        "Te ofrecemos un reembolso del 30% y te quedas el producto.", Action.PARTIAL_REFUND, params, MAX_CHARS
    )
    assert any("porcentaje distinto" in r for r in wrong_pct)
    amount_only = check_message(
        "Te reembolsamos $240 y te quedas el producto.", Action.PARTIAL_REFUND, params, MAX_CHARS
    )
    assert amount_only == []
    wrong_amount = check_message(
        "Te reembolsamos $300 de tu compra de $1,200.", Action.PARTIAL_REFUND, params, MAX_CHARS
    )
    assert any("monto o porcentaje exacto" in r for r in wrong_amount)


def test_redact_pii_before_llm():
    from copiloto.drafting.guardrails import redact_pii

    raw = "escríbeme a juan.perez@gmail.com o al 55 1234 5678, mi tarjeta 4111 1111 1111 1111, ve www.x.com"
    red = redact_pii(raw)
    assert "gmail" not in red and "5678" not in red and "4111" not in red and "www." not in red
    assert "[correo]" in red and "[teléfono]" in red and "[número]" in red and "[enlace]" in red

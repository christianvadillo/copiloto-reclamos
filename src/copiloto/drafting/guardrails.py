"""drafting/guardrails.py — reglas deterministas que TODO mensaje debe pasar antes de guardarse.

Corren sobre el mensaje ya redactado (por LLM o por plantilla) y son la última línea de
defensa: Mercado Libre prohíbe sacar la conversación de la plataforma (correo, teléfono,
redes, URLs) y penaliza amenazas de mediación; nosotros además exigimos coherencia con la
acción que se va a ejecutar (un parcial sin el monto exacto, o una defensa que promete
reembolso, confunden al comprador y generan un reclamo sobre el reclamo).

Cada chequeo es una función pura `texto -> razón | None`; `check_message` solo los junta, así
se prueban uno por uno y se agregan nuevos sin tocar los demás.
"""

from __future__ import annotations

import re

from copiloto.domain import Action
from copiloto.taxonomy import normalize_text

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b[a-z0-9-]+\.(com|mx|net|org|info|io|co|shop)\b", re.IGNORECASE)

_OFF_PLATFORM_WORDS = (
    "whatsapp",
    "whats app",
    "wasap",
    "telegram",
    "facebook",
    "instagram",
    "messenger",
    "signal",
    "tiktok",
    "numero personal",
    "mi numero",
    "fuera de mercado libre",
)
_MEDIATION_THREAT_WORDS = (
    "si no aceptas",
    "si no aceptás",
    "te vamos a reportar",
    "abriremos una mediacion",
    "abriremos una disputa",
    "escalaremos a mediacion",
    "iremos a mediacion",
    "tomaremos acciones legales",
    "denuncia formal",
)
# Cualquier mención (aunque sea para negarlo) hace caer la defensa: es más barato reintentar
# la redacción que arriesgar que el comprador lea una promesa de reembolso entre líneas.
_REFUND_WORD_STEMS = ("reembols", "devolucion", "reintegr", "devuelv")


def _check_length(text: str, max_chars: int) -> str | None:
    if not text.strip():
        return "el mensaje está vacío"
    if len(text) > max_chars:
        return f"el mensaje tiene {len(text)} caracteres; el máximo es {max_chars}"
    return None


def _check_email(text: str) -> str | None:
    return "contiene una dirección de correo electrónico" if _EMAIL_RE.search(text) else None


def _check_phone(text: str) -> str | None:
    """Cualquier corrida de exactamente 10 dígitos (u 11 con `52` de país), separadores comunes
    aparte, sin más dígitos pegados: así no marca guías o montos largos por accidente."""
    compact = re.sub(r"[\s\-.]", "", text)
    return "contiene un número telefónico" if re.search(r"(?<!\d)(52)?\d{10}(?!\d)", compact) else None


def _check_url(text: str) -> str | None:
    return "contiene una URL o un dominio" if (_URL_RE.search(text) or _DOMAIN_RE.search(text)) else None


def _check_off_platform(text: str) -> str | None:
    norm = normalize_text(text)
    if any(normalize_text(w) in norm for w in _OFF_PLATFORM_WORDS):
        return "menciona un canal fuera de Mercado Libre (redes sociales, WhatsApp, Telegram...)"
    return None


def _check_mediation_threat(text: str) -> str | None:
    norm = normalize_text(text)
    if any(normalize_text(w) in norm for w in _MEDIATION_THREAT_WORDS):
        return "contiene una amenaza de mediación o de acciones legales"
    return None


_PCT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s?%")
_MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")


def _check_partial_amount(text: str, action: Action, params: dict | None = None) -> str | None:
    """El parcial debe citar EXACTAMENTE lo que se va a ejecutar: el % de `params["pct"]` o el
    monto de `params["amount"]` (±$1). Otro porcentaje en el texto es una promesa distinta a la
    que va a ML y se rechaza; otros montos se toleran (p. ej. el total de la compra)."""
    if action is not Action.PARTIAL_REFUND:
        return None
    params = params or {}
    pcts = {round(float(x.replace(",", "."))) for x in _PCT_RE.findall(text)}
    amounts = [float(x.replace(",", "")) for x in _MONEY_RE.findall(text)]
    pct = params.get("pct")
    amount = params.get("amount")
    if pct is None and amount is None:
        return (
            None if (pcts or amounts) else "es un reembolso parcial pero no menciona el monto exacto ni el porcentaje"
        )
    expected_pct = round(float(pct) * 100) if pct is not None else None
    if expected_pct is not None and pcts - {expected_pct}:
        return f"menciona un porcentaje distinto al que se ejecutará ({expected_pct}%)"
    ok_pct = expected_pct is not None and expected_pct in pcts
    ok_amount = amount is not None and any(abs(a - float(amount)) < 1.0 for a in amounts)
    if not (ok_pct or ok_amount):
        target = f"{expected_pct}%" if expected_pct is not None else f"${float(amount):,.2f}"
        return f"es un reembolso parcial pero no menciona el monto o porcentaje exacto ({target})"
    return None


def _check_defend_no_promise(text: str, action: Action) -> str | None:
    if action is not Action.DEFEND:
        return None
    norm = normalize_text(text)
    if any(stem in norm for stem in _REFUND_WORD_STEMS):
        return "defiende la venta pero menciona reembolso o devolución"
    return None


def redact_pii(text: str) -> str:
    """Quita del texto del comprador lo que no debe salir hacia el LLM: correos, URLs,
    teléfonos y corridas largas de dígitos (tarjetas, cuentas, CLABE)."""
    text = _EMAIL_RE.sub("[correo]", text)
    text = _URL_RE.sub("[enlace]", text)
    # Primero las corridas largas (tarjeta/CLABE) para que no se confundan con un teléfono.
    text = re.sub(r"(?<!\d)(?:\d[\s-]?){12,19}\d(?!\d)", "[número]", text)
    return re.sub(r"(?<!\d)(?:\+?52[\s.-]?)?(?:\d[\s.-]?){9}\d(?!\d)", "[teléfono]", text)


def check_message(text: str, action: Action, params: dict | None, max_chars: int) -> list[str]:
    """Todas las razones por las que este mensaje NO se puede enviar tal cual. Lista vacía =
    pasa. `params` son los de la acción recomendada (p. ej. `{"pct": 0.2, "amount": 240.0}`)."""
    checks = (
        _check_length(text, max_chars),
        _check_email(text),
        _check_phone(text),
        _check_url(text),
        _check_off_platform(text),
        _check_mediation_threat(text),
        _check_partial_amount(text, action, params),
        _check_defend_no_promise(text, action),
    )
    return [c for c in checks if c is not None]

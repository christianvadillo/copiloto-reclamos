"""taxonomy.py — traduce el vocabulario de Mercado Libre al del copiloto.

Fuente de verdad de los strings de ML (verificado contra developers.mercadolibre.com.ar,
2025-2026): prefijos de `reason_id` PNR (no recibido), PDD (diferente o defectuoso),
CS (compra cancelada); `type` mediations/return/fulfillment/ml_case/cancel_sale/
cancel_purchase/change/service; `stage` claim/dispute/recontact/none/stale.

La clasificación es por reglas y devuelve una confianza. Debajo de `LLM_THRESHOLD` el
pipeline puede pedir una segunda opinión al LLM; la regla gana si el LLM no está disponible.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from copiloto.domain import Action, Category

LLM_THRESHOLD = 0.6

# Orden importa: "llegó en buenas condiciones pero no lo quiero" es arrepentimiento aunque
# mencione el estado; "le faltan piezas" es incompleto aunque diga "no funciona".
_KEYWORDS: tuple[tuple[Category, tuple[str, ...]], ...] = (
    (
        Category.DEVOLUCION,
        (
            "repentant",
            "arrepent",
            "no lo quiero",
            "ya no lo quiero",
            "no la quiero",
            "cambie de opinion",
            "no me gusto",
            "no lo necesito",
            "ya no lo necesito",
            "regret",
            "changed my mind",
        ),
    ),
    (
        Category.INCOMPLETO,
        ("incomplet", "falta", "faltan", "missing", "sin accesorio", "sin cargador", "pieza", "piezas"),
    ),
    (
        Category.DEFECTUOSO,
        (
            "damaged",
            "danad",
            "roto",
            "rota",
            "rompi",
            "defect",
            "no funciona",
            "no enciende",
            "no prende",
            "no carga",
            "falla",
            "broken",
            "not working",
            "doesn't work",
            "estrellad",
            "golpead",
            "quebrad",
            "mal estado",
        ),
    ),
    (
        Category.DIFERENTE,
        (
            "different",
            "diferente",
            "distint",
            "otro modelo",
            "otro color",
            "otra talla",
            "talla",
            "not as described",
            "no es el que",
            "no es lo que",
            "no corresponde",
            "equivocad",
            "wrong",
            "no coincide",
            "falsificad",
            "imitacion",
            "pirata",
        ),
    ),
)

_CANCEL_TYPES = frozenset({"cancel_purchase", "cancel_sale"})


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def _keyword_category(text: str) -> Category | None:
    norm = normalize_text(text)
    if not norm:
        return None
    for cat, words in _KEYWORDS:
        if any(w in norm for w in words):
            return cat
    return None


@dataclass(frozen=True)
class Classification:
    category: Category
    confidence: float
    source: str  # "type" | "reason_prefix" | "reason_text" | "buyer_text" | "fallback" | "llm"

    @property
    def needs_second_opinion(self) -> bool:
        return self.confidence < LLM_THRESHOLD


def classify(
    reason_id: str | None,
    claim_type: str | None = None,
    reason_name: str | None = None,
    reason_detail: str | None = None,
    buyer_text: str | None = None,
) -> Classification:
    """Categoría del reclamo a partir de lo que devuelve la API (y el texto del comprador)."""
    rid = (reason_id or "").upper()
    if claim_type in _CANCEL_TYPES or rid.startswith("CS"):
        return Classification(Category.CANCELACION, 0.9, "type" if claim_type in _CANCEL_TYPES else "reason_prefix")
    if rid.startswith("PNR"):
        return Classification(Category.NO_RECIBIDO, 0.95, "reason_prefix")

    # PDD, return, change o desconocido: afinar con el texto del motivo y luego del comprador.
    cat = _keyword_category(f"{reason_name or ''} {reason_detail or ''}")
    if cat is not None:
        return Classification(cat, 0.85, "reason_text")
    cat = _keyword_category(buyer_text)
    if cat is not None:
        return Classification(cat, 0.6, "buyer_text")
    if claim_type == "return":
        return Classification(Category.DEVOLUCION, 0.5, "fallback")
    if rid.startswith("PDD"):
        # PDD sin más pistas: defectuoso es el caso más común y el más caro de subestimar.
        return Classification(Category.DEFECTUOSO, 0.4, "fallback")
    return Classification(Category.OTRO, 0.2, "fallback")


# ── available_actions de ML → acciones del copiloto ────────────────────────────────────────

_MESSAGE_ACTIONS = frozenset({Action.DEFEND, Action.INFORM_TRACKING, Action.EXCHANGE, Action.RESEND})


def map_available_actions(ml_actions: Iterable[str], stage: str | None = None) -> frozenset[Action]:
    """Qué resoluciones puede ejecutar el vendedor ahora mismo según la API.

    Cambio y reenvío no tienen endpoint propio: se proponen por mensaje y la logística es
    manual; por eso dependen de poder escribirle al comprador. En `dispute` solo queda
    hablar con el mediador (defender) y reembolsar.
    """
    acts = set(ml_actions)
    out: set[Action] = set()
    if "refund" in acts:
        out.add(Action.REFUND_FULL)
    if acts & {"allow_return", "allow_return_label"}:
        out.add(Action.RETURN_REFUND)
    if "allow_partial_refund" in acts:
        out.add(Action.PARTIAL_REFUND)
    if "send_message_to_complainant" in acts and stage != "dispute":
        out |= _MESSAGE_ACTIONS
    if "send_message_to_mediator" in acts:
        out.add(Action.DEFEND)
    return frozenset(out)


@dataclass(frozen=True)
class MLCall:
    """Cómo se ejecuta una acción en la API. `path` lleva `{claim_id}`."""

    method: str
    path: str
    body: dict | None = None
    sends_message: bool = False


def execution_plan(action: Action, pct: float | None = None, stage: str | None = None) -> list[MLCall]:
    """Llamadas a la API para ejecutar la acción. El mensaje redactado siempre acompaña
    (explica la resolución al comprador); en `dispute` se dirige al mediador."""
    base = "/post-purchase/v1/claims/{claim_id}"
    receiver = "mediator" if stage == "dispute" else "complainant"
    msg = MLCall("POST", f"{base}/actions/send-message", {"receiver_role": receiver}, sends_message=True)
    if action is Action.REFUND_FULL:
        return [msg, MLCall("POST", f"{base}/expected-resolutions/refund")]
    if action is Action.RETURN_REFUND:
        return [msg, MLCall("POST", f"{base}/expected-resolutions/allow-return")]
    if action is Action.PARTIAL_REFUND:
        if pct is None or not (0 < pct < 1):
            raise ValueError("reembolso parcial requiere 0 < pct < 1 (100% va por /refund)")
        body = {"percentage": round(pct * 100)}
        return [msg, MLCall("POST", f"{base}/expected-resolutions/partial-refund", body)]
    # Defender, informar rastreo, proponer cambio o reenvío: solo mensaje (+ evidencia aparte).
    return [msg]

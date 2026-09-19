"""evidence.py — qué tan fuerte es el caso del vendedor, en [0, 1].

El score alimenta dos cosas: P(ganar la mediación) y P(que el comprador escale si el vendedor
defiende). Se indexa en buckets (fuerte/media/débil) para que el historial propio llene celdas.

Los pesos son juicio experto y están a la vista a propósito: cada punto del score viene con
su razón en español para que el vendedor vea POR QUÉ el copiloto cree que puede ganar.
La recolección (API de envíos, mensajes, fotos) vive en el pipeline; aquí solo se puntúa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from copiloto.domain import Category

ML_LOGISTICS = frozenset({"fulfillment", "cross_docking", "drop_off", "xd_drop_off", "self_service", "turbo"})


@dataclass(frozen=True)
class EvidenceFacts:
    shipment_status: str | None = None  # delivered, shipped, not_delivered, cancelled, ...
    shipment_substatus: str | None = None
    logistic_type: str | None = None  # fulfillment (Full), cross_docking, drop_off, ...
    tracking_number: str | None = None
    delivered_at: datetime | None = None
    shipped_at: datetime | None = None
    claim_created_at: datetime | None = None
    packing_photos: int = 0  # fotos del empaque/producto antes de enviar
    listing_attributes_match: bool | None = None  # lo enviado coincide con la publicación
    serial_or_batch_recorded: bool = False  # serie/lote anotado al despachar
    buyer_acknowledged_receipt: bool = False  # el comprador dijo que lo recibió
    cancellation_after_shipping: bool = False  # pidió cancelar con el paquete ya en camino
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def delivered(self) -> bool:
        return self.shipment_status == "delivered"

    @property
    def in_transit(self) -> bool:
        return self.shipment_status in {"shipped", "ready_to_ship", "handling"}

    @property
    def days_delivered_to_claim(self) -> float | None:
        if self.delivered_at and self.claim_created_at:
            return (self.claim_created_at - self.delivered_at).total_seconds() / 86400
        return None


@dataclass(frozen=True)
class EvidenceScore:
    score: float
    reasons: tuple[str, ...]


def score_evidence(category: Category, f: EvidenceFacts) -> EvidenceScore:
    pts: list[tuple[float, str]] = []

    if category is Category.NO_RECIBIDO:
        if f.delivered:
            pts.append((0.45, "el envío figura como entregado"))
            if f.logistic_type in ML_LOGISTICS:
                pts.append((0.15, "entregado por logística de Mercado Envíos (rastreo de ML)"))
            if f.delivered_at and f.claim_created_at and f.delivered_at < f.claim_created_at:
                pts.append((0.10, "la entrega es anterior a la apertura del reclamo"))
        elif f.in_transit:
            pts.append((0.10, "el paquete sigue en tránsito"))
        if f.buyer_acknowledged_receipt:
            pts.append((0.20, "el comprador reconoció haberlo recibido en mensajes"))
        if f.packing_photos:
            pts.append((0.05, f"{f.packing_photos} foto(s) del despacho"))

    elif category in (Category.DEFECTUOSO, Category.DIFERENTE, Category.INCOMPLETO):
        if f.packing_photos:
            pts.append((0.35, f"{f.packing_photos} foto(s) del producto/empaque antes de enviar"))
        if f.listing_attributes_match:
            pts.append((0.15, "lo enviado coincide con los atributos de la publicación"))
        if f.serial_or_batch_recorded:
            pts.append((0.15, "número de serie/lote registrado al despachar"))
        gap = f.days_delivered_to_claim
        if gap is not None and gap > 10:
            pts.append((0.15, f"el reclamo llegó {gap:.0f} días después de la entrega"))
        if f.logistic_type in ML_LOGISTICS and f.delivered:
            pts.append((0.05, "entrega trazada por Mercado Envíos"))

    elif category is Category.DEVOLUCION:
        # La política de devolución favorece al comprador: la evidencia casi no mueve nada.
        pts.append((0.10, "arrepentimiento: la política de devoluciones favorece al comprador"))
        if f.packing_photos:
            pts.append((0.10, "fotos para verificar el estado al regresar"))

    elif category is Category.CANCELACION:
        if f.cancellation_after_shipping:
            pts.append((0.50, "pidió cancelar con el paquete ya despachado"))
        if f.delivered:
            pts.append((0.35, "el paquete ya fue entregado"))

    else:
        if f.delivered:
            pts.append((0.20, "el envío figura como entregado"))
        if f.packing_photos:
            pts.append((0.15, "fotos del despacho"))

    score = min(1.0, sum(p for p, _ in pts))
    return EvidenceScore(score=round(score, 3), reasons=tuple(r for _, r in pts))

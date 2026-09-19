"""domain.py — vocabulario del copiloto: categorías, acciones y contenedores de datos.

Todo lo demás (cliente de la API, webhook, redactor, recomendador) habla en estos términos.
Nada aquí hace I/O. Los nombres de Mercado Libre (reason_id, stage, available_actions) se
traducen a este vocabulario en `taxonomy.py`; el resto del código no debería ver strings de ML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Category(StrEnum):
    """Qué le pasó al comprador. Decide qué acciones tienen sentido y qué priors aplican."""

    NO_RECIBIDO = "no_recibido"  # PNR: no llegó / llegó a otra dirección / tracking detenido
    DEFECTUOSO = "defectuoso"  # PDD: roto, dañado, no funciona
    DIFERENTE = "diferente"  # PDD: no es lo que compró (modelo, color, talla, descripción)
    INCOMPLETO = "incompleto"  # PDD: faltan piezas/accesorios/unidades
    DEVOLUCION = "devolucion"  # arrepentimiento: ya no lo quiere (devolución gratis de ML)
    CANCELACION = "cancelacion"  # quiere cancelar antes/durante el envío
    OTRO = "otro"  # no clasificable con confianza → siempre revisión humana


class Action(StrEnum):
    """Resoluciones que el copiloto sabe evaluar. `taxonomy.map_available_actions` decide cuáles
    están permitidas en cada reclamo según los `available_actions` que devuelve la API."""

    REFUND_FULL = "refund_full"  # reembolso total; el comprador se queda el producto
    RETURN_REFUND = "return_refund"  # permitir devolución; reembolso al recibirla
    PARTIAL_REFUND = "partial_refund"  # reembolso parcial; el comprador se queda el producto
    EXCHANGE = "exchange"  # cambio: se envía otra unidad y regresa la original
    RESEND = "resend"  # reenvío de la unidad o de la pieza faltante, sin devolución
    INFORM_TRACKING = "inform_tracking"  # no recibido y en tránsito: informar rastreo y ETA
    DEFEND = "defend"  # responder con evidencia sin conceder (asume riesgo de mediación)


# Acciones que mueven dinero o inventario. Nunca se ejecutan sin aprobación salvo política
# explícita (modo auto + monto bajo tope + confianza alta).
MONEY_ACTIONS: frozenset[Action] = frozenset(
    {Action.REFUND_FULL, Action.RETURN_REFUND, Action.PARTIAL_REFUND, Action.EXCHANGE, Action.RESEND}
)


class EvidenceStrength(StrEnum):
    """Buckets de fuerza de la evidencia del vendedor (ver `evidence_bucket`)."""

    FUERTE = "fuerte"
    MEDIA = "media"
    DEBIL = "debil"


class RepStatus(StrEnum):
    """Respuesta de `GET /post-purchase/v1/claims/{id}/affects-reputation`."""

    AFFECTED = "affected"
    NOT_AFFECTED = "not_affected"
    NOT_APPLIES = "not_applies"
    UNKNOWN = "unknown"


def evidence_bucket(score: float) -> EvidenceStrength:
    """Score continuo en [0,1] → bucket. Los priors de mediación se indexan por bucket para que
    el historial del vendedor alcance a llenar celdas (con score continuo nunca se repetiría)."""
    if score >= 0.66:
        return EvidenceStrength.FUERTE
    if score >= 0.33:
        return EvidenceStrength.MEDIA
    return EvidenceStrength.DEBIL


@dataclass(frozen=True)
class Economics:
    """Montos del reclamo en MXN, medidos contra el escenario base "la venta se sostiene".

    Los costos hundidos (envío de ida ya pagado, comisión ya cobrada si no se devuelve) no
    cambian entre acciones y por eso no aparecen: solo importa lo que difiere entre opciones.
    """

    order_amount: float  # V: lo pagado por las unidades reclamadas
    unit_cost: float  # C: costo de reposición de una unidad (COGS)
    sale_fee: float = 0.0  # F: cargo por venta que cobró ML por esas unidades
    fee_refunded_on_refund: bool = True  # ¿ML devuelve el cargo por venta al reembolsar?
    return_shipping_cost: float = 0.0  # guía de devolución si la paga el vendedor
    resend_shipping_cost: float = 0.0  # envío de una unidad de reemplazo o pieza
    resend_unit_cost: float | None = None  # costo de lo que se reenvía (None → unit_cost)
    handling_cost: float = 0.0  # mano de obra de procesar devolución/cambio
    mediation_labor_cost: float = 0.0  # horas del vendedor en una mediación, valuadas
    exchange_available: bool = False  # hay stock para cambio/reenvío

    @property
    def net_refund_loss(self) -> float:
        """Lo que el vendedor deja de conservar si reembolsa el total."""
        fee_back = self.sale_fee if self.fee_refunded_on_refund else 0.0
        return max(0.0, self.order_amount - fee_back)


@dataclass(frozen=True)
class ReputationState:
    """Estado del termómetro del vendedor, para ponerle precio (λ) a un reclamo que afecta.

    `claims_threshold_rate` es la tasa máxima que permite conservar el nivel actual; la
    holgura H es cuántos reclamos que afectan caben todavía antes de cruzarla.
    """

    sales_window: int  # ventas completadas en la ventana de cálculo (60 días por defecto)
    affecting_claims_window: int  # reclamos que ya afectan dentro de esa ventana
    claims_threshold_rate: float  # p. ej. 0.02 → máximo 2% de reclamos
    level_drop_cost: float  # L: MXN que dejas de ganar si pasas una ventana completa abajo
    window_days: float = 60.0  # 60 días; 365 si hubo menos de 40 ventas en 60 días (MLM)
    # Tasa diaria de reclamos que cuentan ~ Gamma(shape, rate); se actualiza con la ventana.
    rate_prior_shape: float = 0.5
    rate_prior_rate: float = 10.0
    base_cost_per_claim: float = 0.0  # costo "blando" por reclamo aunque no cruce umbral
    affecting_claim_ages_days: tuple[float, ...] | None = None  # edades exactas si se conocen
    grid_steps: int = 30  # resolución de la integral sobre la ventana
    min_sales_for_rate: int = 11  # ML exige >10 ventas para calcular la tasa de reclamos
    # Reglas de conteo (SUPUESTOS a calibrar con `affects_reputation` al cierre de cada caso):
    won_mediation_counts: float = 0.0  # fracción que cuenta si ML falla a favor del vendedor
    desist_counts_in_incentive: float = 0.5  # defender y que el comprador desista, con ventana 48h

    @property
    def max_affecting_claims(self) -> int:
        return int(self.claims_threshold_rate * self.sales_window + 1e-9)

    @property
    def headroom(self) -> int:
        return self.max_affecting_claims - self.affecting_claims_window


@dataclass(frozen=True)
class ClaimContext:
    """Todo lo que el recomendador necesita de un reclamo, ya traducido y sin I/O."""

    claim_id: str
    category: Category
    economics: Economics
    evidence_score: float = 0.0  # fuerza del caso del vendedor en [0,1]
    allowed_actions: frozenset[Action] = frozenset(Action)
    partial_refund_offers: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
    shipment_in_transit: bool = False  # para NO_RECIBIDO: aún puede llegar
    shipment_delivered: bool = False
    fulfillment_by_ml: bool = False  # Full: ML responde por la logística
    days_open: float = 0.0
    rep_status: RepStatus = RepStatus.UNKNOWN
    has_incentive: bool = False  # ventana de 48 h: resolver bien a tiempo evita que afecte
    hours_left_incentive: float | None = None
    buyer_expected: frozenset[str] = frozenset()  # expected-resolutions del comprador


@dataclass(frozen=True)
class ActionEstimate:
    action: Action
    expected_cost: float  # E[costo] promediado sobre la posterior (MXN)
    cost_p05: float  # intervalo de credibilidad 90% del costo esperado
    cost_p95: float
    prob_best: float  # P(esta acción es la de menor costo esperado)
    params: dict = field(default_factory=dict)  # p. ej. {"pct": 0.2}
    escalation_prob: float = 0.0  # P(mediación | acción), media posterior


@dataclass(frozen=True)
class Recommendation:
    claim_id: str
    category: Category
    action: Action
    params: dict
    expected_cost: float
    prob_best: float
    ranking: tuple[ActionEstimate, ...]
    reputation_lambda: float  # λ: MXN por reclamo que afecta, dado el estado actual
    reputation_headroom: int
    extra_bad_days: float  # días extra en nivel bajo que este reclamo causa si cuenta
    rationale: tuple[str, ...]
    requires_approval: bool
    approval_reasons: tuple[str, ...] = ()
    created_at: datetime | None = None

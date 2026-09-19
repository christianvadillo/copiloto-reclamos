"""recommender.py — elige la resolución de menor costo esperado para un reclamo.

    costo(acción) = dinero(acción) + λ · P(el reclamo cuenta en la reputación | acción)

`dinero` son los MXN que salen o no entran respecto al escenario "la venta se sostiene"
(reembolsos, guías, unidades de reemplazo, menos lo que se recupera de una devolución).
λ es el precio sombra de un reclamo que afecta (ver `reputation.py`): casi cero con holgura,
enorme pegado al umbral del termómetro.

Tres regímenes de reputación, según `affects-reputation` del reclamo:
  - "none":      not_applies, o ya `affected` sin incentivo → el daño no depende de lo que
                  hagas ahora (o no existe): se minimiza solo dinero.
  - "incentive": `has_incentive` → resolver satisfactoriamente dentro de las 48 h evita que
                  afecte. Conceder rápido vale oro; una oferta rechazada empuja la solución
                  fuera de la ventana con probabilidad `late`.
  - "general":   el reclamo cuenta si termina mediado y el vendedor pierde.

Incertidumbre: cada probabilidad es una Beta (priors + historial propio, ver `priors.py`).
Se promedian 2,000 muestras de la posterior: E[costo] integra la incertidumbre, el IC90%
muestra cuánto se mueve el costo esperado con lo que NO sabemos, y P(mejor) dice qué tan
seguido esta acción gana. P(mejor) baja = decisión cerrada → revisión humana.

Esto no es un modelo del comprador individual: es un modelo del COSTO de cada política dado
lo que el historial dice de compradores parecidos. Sus supuestos (reglas de conteo, λ, priors)
están nombrados y son configurables; el historial los va reemplazando.
"""

from __future__ import annotations

import random
import statistics
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from copiloto.decision.priors import Key, PriorBook, isotonic_increasing, pct_key
from copiloto.decision.reputation import ShadowPrice, shadow_price
from copiloto.domain import (
    MONEY_ACTIONS,
    Action,
    ActionEstimate,
    Category,
    ClaimContext,
    EvidenceStrength,
    Recommendation,
    RepStatus,
    ReputationState,
    evidence_bucket,
)

ACTION_LABELS: dict[Action, str] = {
    Action.REFUND_FULL: "Reembolso total (se queda el producto)",
    Action.RETURN_REFUND: "Devolución con reembolso",
    Action.PARTIAL_REFUND: "Reembolso parcial",
    Action.EXCHANGE: "Cambio por otra unidad",
    Action.RESEND: "Reenvío (unidad o pieza faltante)",
    Action.INFORM_TRACKING: "Informar rastreo y fecha estimada",
    Action.DEFEND: "Defender con evidencia",
}

CONCESSIONS = frozenset(
    {Action.REFUND_FULL, Action.RETURN_REFUND, Action.PARTIAL_REFUND, Action.EXCHANGE, Action.RESEND}
)

# Llaves de `Params.esc`, en el orden de declaración de `Action`. Tupla y no set a propósito:
# `_draw_params` consume el RNG en este orden, y un set de StrEnum se itera según el hash de
# `str`, que cambia por proceso (PYTHONHASHSEED) → el mismo claim_id repartiría los mismos
# sorteos a otras acciones al reiniciar el proceso.
_ESC_ACTIONS: tuple[Action, ...] = tuple(a for a in Action if a in CONCESSIONS or a is Action.INFORM_TRACKING)


@dataclass(frozen=True)
class Policy:
    """Cuánto se le permite hacer solo al copiloto."""

    mode: str = "shadow"  # shadow: solo recomienda | approve: todo con visto bueno | auto
    auto_max_amount: float = 1500.0  # MXN; arriba de esto una acción de dinero pide aprobación
    min_prob_best: float = 0.60
    auto_actions: frozenset[Action] = frozenset({Action.INFORM_TRACKING, Action.RETURN_REFUND})
    escalation_bump: float = 0.05  # Δ P(mediación) cuando una primera oferta fue rechazada
    late_if_rejected: float = 0.5  # P(la solución tras un rechazo cae fuera de la ventana 48 h)
    n_draws: int = 2000
    seed: int | None = None


def category_actions(ctx: ClaimContext) -> set[Action]:
    """Acciones que tienen sentido por categoría (antes de cruzar con lo que permite la API)."""
    stock = ctx.economics.exchange_available
    cat = ctx.category
    acts: set[Action]
    if cat is Category.NO_RECIBIDO:
        acts = {Action.REFUND_FULL}
        if stock:
            acts.add(Action.RESEND)
        if ctx.shipment_in_transit:
            acts.add(Action.INFORM_TRACKING)
        if ctx.shipment_delivered:
            acts.add(Action.DEFEND)
    elif cat in (Category.DEFECTUOSO, Category.DIFERENTE):
        acts = {Action.RETURN_REFUND, Action.PARTIAL_REFUND, Action.REFUND_FULL, Action.DEFEND}
        if stock:
            acts.add(Action.EXCHANGE)
    elif cat is Category.INCOMPLETO:
        acts = {Action.PARTIAL_REFUND, Action.RETURN_REFUND, Action.REFUND_FULL, Action.DEFEND}
        if stock:
            acts.add(Action.RESEND)
    elif cat is Category.DEVOLUCION:
        acts = {Action.RETURN_REFUND, Action.PARTIAL_REFUND, Action.DEFEND}
    elif cat is Category.CANCELACION:
        acts = {Action.REFUND_FULL}
        if ctx.shipment_in_transit:
            acts.add(Action.INFORM_TRACKING)
        if ctx.shipment_delivered:
            acts.add(Action.DEFEND)
    else:
        acts = {Action.REFUND_FULL, Action.RETURN_REFUND, Action.PARTIAL_REFUND, Action.DEFEND}
    return acts


def candidate_actions(ctx: ClaimContext) -> list[Action]:
    """Acciones sensatas ∩ lo que permite la API. Defender exige evidencia al menos media: sin
    base para disputar el reclamo, "defender" es negarle al comprador lo que probablemente le
    corresponde, y el copiloto no lo recomienda aunque en dinero salga barato. Única excepción:
    que la API no deje otra cosa (p. ej. en disputa solo queda hablar con el mediador)."""
    acts = category_actions(ctx) & set(ctx.allowed_actions)
    if Action.DEFEND in acts and len(acts) > 1 and evidence_bucket(ctx.evidence_score) is EvidenceStrength.DEBIL:
        acts.discard(Action.DEFEND)
    if not acts:
        # La API no deja hacer nada de lo sensato: al menos responder por mensaje.
        acts = {Action.DEFEND} if Action.DEFEND in ctx.allowed_actions else {Action.REFUND_FULL}
    return sorted(acts, key=lambda a: list(Action).index(a))


def reputation_regime(ctx: ClaimContext) -> str:
    if ctx.rep_status is RepStatus.NOT_APPLIES:
        return "none"
    if ctx.has_incentive:
        return "incentive"
    if ctx.rep_status is RepStatus.AFFECTED:
        return "none"  # ya cuenta y no hay ventana: nada de lo que hagas lo cambia
    return "general"


# ── Parámetros (una muestra de la posterior, o sus medias) ──────────────────────────────────


@dataclass
class Params:
    esc: dict[Action, float] = field(default_factory=dict)
    esc_defend: float = 0.5
    win: float = 0.2
    accept_partial: dict[float, float] = field(default_factory=dict)
    accept_exchange: float = 0.4
    accept_resend: float = 0.4
    recovery: float = 0.5
    coverage: float = 0.1
    resolve_inform: float = 0.4


def _keys(ctx: ClaimContext) -> dict[str, Key]:
    cat = ctx.category.value
    bucket = evidence_bucket(ctx.evidence_score).value
    return {
        "esc_defend": ("esc_defend", cat, bucket),
        "win": ("win", cat, bucket),
        "accept_exchange": ("accept_exchange", cat, ""),
        "accept_resend": ("accept_resend", cat, ""),
        "recovery": ("recovery", cat, ""),
        "coverage": ("coverage", cat, "full" if ctx.fulfillment_by_ml else ""),
        "resolve_inform": ("resolve_inform", cat, ""),
    }


def _draw_params(book: PriorBook, ctx: ClaimContext, rng: random.Random | None) -> Params:
    """rng=None → medias posteriores (para escoger el % óptimo y reportar)."""

    def val(key: Key) -> float:
        post = book.posterior(key)
        return post.mean if rng is None else post.sample(rng)

    cat = ctx.category.value
    k = _keys(ctx)
    p = Params(
        esc={a: val(("esc", cat, a.value)) for a in _ESC_ACTIONS},
        esc_defend=val(k["esc_defend"]),
        win=val(k["win"]),
        accept_exchange=val(k["accept_exchange"]),
        accept_resend=val(k["accept_resend"]),
        recovery=val(k["recovery"]),
        coverage=val(k["coverage"]),
        resolve_inform=val(k["resolve_inform"]),
    )
    offers = sorted(ctx.partial_refund_offers)
    if offers:
        raw = [val(("accept_partial", cat, pct_key(x))) for x in offers]
        weights = [book.posterior(("accept_partial", cat, pct_key(x))).strength for x in offers]
        p.accept_partial = dict(zip(offers, isotonic_increasing(raw, weights), strict=True))
    _align_with_buyer(p, ctx.buyer_expected)
    return p


def _align_with_buyer(p: Params, expected: frozenset[str]) -> None:
    """Heurística: si el comprador ya pidió esa resolución, rechazarla es menos probable.
    Reduce la probabilidad de rechazo 30%; con solo 'refund', el parcial pierde 20%."""
    if not expected:
        return

    def ease(q: float) -> float:
        return 1.0 - (1.0 - q) * 0.7

    if "change_product" in expected:
        p.accept_exchange = ease(p.accept_exchange)
    if "product" in expected:
        p.accept_resend = ease(p.accept_resend)
    if "partial_refund" in expected:
        p.accept_partial = {x: ease(q) for x, q in p.accept_partial.items()}
    elif expected == frozenset({"refund"}):
        p.accept_partial = {x: q * 0.8 for x, q in p.accept_partial.items()}


# ── Costos ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Cost:
    money: float
    counted: float  # P(el reclamo cuenta en la reputación)
    escalation: float  # P(mediación)

    def total(self, lam: float) -> float:
        return self.money + lam * self.counted


class _CostModel:
    def __init__(
        self,
        ctx: ClaimContext,
        rep: ReputationState,
        policy: Policy,
        allowed: Iterable[Action],
    ) -> None:
        self.ctx = ctx
        self.rep = rep
        self.policy = policy
        self.allowed = set(allowed)
        self.regime = reputation_regime(ctx)
        hours = ctx.hours_left_incentive
        self.late = 1.0 if (hours is not None and hours < 12) else policy.late_if_rejected

    # Reglas de conteo -----------------------------------------------------------------------

    def _counted_concession(self, esc: float, delayed: bool) -> float:
        if self.regime == "none":
            return 0.0
        if self.regime == "incentive" and delayed:
            return self.late + (1.0 - self.late) * esc
        return esc

    def _counted_mediation(self, win: float) -> float:
        if self.regime == "none":
            return 0.0
        return win * self.rep.won_mediation_counts + (1.0 - win)

    def _counted_desist(self) -> float:
        return self.rep.desist_counts_in_incentive if self.regime == "incentive" else 0.0

    # Acciones simples -----------------------------------------------------------------------

    def _mediation_money(self, p: Params) -> float:
        e = self.ctx.economics
        return (1.0 - p.win) * e.net_refund_loss * (1.0 - p.coverage) + e.mediation_labor_cost

    def refund_full(self, p: Params, bump: float = 0.0, delayed: bool = False) -> _Cost:
        esc = min(1.0, p.esc[Action.REFUND_FULL] + bump)
        return _Cost(
            money=self.ctx.economics.net_refund_loss,
            counted=self._counted_concession(esc, delayed),
            escalation=esc,
        )

    def return_refund(self, p: Params, bump: float = 0.0, delayed: bool = False) -> _Cost:
        e = self.ctx.economics
        esc = min(1.0, p.esc[Action.RETURN_REFUND] + bump)
        money = e.net_refund_loss + e.return_shipping_cost + e.handling_cost - p.recovery * e.unit_cost
        return _Cost(money=money, counted=self._counted_concession(esc, delayed), escalation=esc)

    def defend(self, p: Params, bump: float = 0.0, delayed: bool = False) -> _Cost:
        esc = min(1.0, p.esc_defend + bump)
        money = esc * self._mediation_money(p)
        counted = esc * self._counted_mediation(p.win) + (1.0 - esc) * self._counted_desist()
        return _Cost(money=money, counted=counted, escalation=esc)

    def _fallback(self, p: Params, options: Iterable[Action], lam: float) -> _Cost:
        """Lo que se hace si la primera oferta no funciona: más tarde y con el comprador más
        molesto (bump en P(mediación))."""
        bump = self.policy.escalation_bump
        simple = {
            Action.REFUND_FULL: self.refund_full,
            Action.RETURN_REFUND: self.return_refund,
            Action.DEFEND: self.defend,
        }
        costs = [simple[a](p, bump, True) for a in options if a in simple and a in self.allowed]
        if not costs:
            costs = [self.refund_full(p, bump, True)]
        return min(costs, key=lambda c: c.total(lam))

    # Acciones compuestas (oferta que el comprador puede rechazar) ---------------------------

    def _offer(self, q: float, accepted: _Cost, fallback: _Cost) -> _Cost:
        return _Cost(
            money=q * accepted.money + (1.0 - q) * fallback.money,
            counted=q * accepted.counted + (1.0 - q) * fallback.counted,
            escalation=q * accepted.escalation + (1.0 - q) * fallback.escalation,
        )

    def partial_refund(self, p: Params, pct: float, lam: float) -> _Cost:
        esc = p.esc[Action.PARTIAL_REFUND]
        accepted = _Cost(
            money=pct * self.ctx.economics.order_amount,
            counted=self._counted_concession(esc, False),
            escalation=esc,
        )
        fb = self._fallback(p, (Action.RETURN_REFUND, Action.REFUND_FULL, Action.DEFEND), lam)
        return self._offer(p.accept_partial.get(pct, 0.0), accepted, fb)

    def exchange(self, p: Params, lam: float) -> _Cost:
        e = self.ctx.economics
        esc = p.esc[Action.EXCHANGE]
        unit = e.resend_unit_cost if e.resend_unit_cost is not None else e.unit_cost
        money = unit + e.resend_shipping_cost + e.return_shipping_cost + e.handling_cost - p.recovery * e.unit_cost
        accepted = _Cost(money=money, counted=self._counted_concession(esc, False), escalation=esc)
        fb = self._fallback(p, (Action.RETURN_REFUND, Action.REFUND_FULL, Action.DEFEND), lam)
        return self._offer(p.accept_exchange, accepted, fb)

    def resend(self, p: Params, lam: float, delayed: bool = False) -> _Cost:
        e = self.ctx.economics
        esc = p.esc[Action.RESEND]
        unit = e.resend_unit_cost if e.resend_unit_cost is not None else e.unit_cost
        accepted = _Cost(
            money=unit + e.resend_shipping_cost,
            counted=self._counted_concession(esc, delayed),
            escalation=esc,
        )
        fb = self._fallback(p, (Action.REFUND_FULL, Action.RETURN_REFUND, Action.DEFEND), lam)
        return self._offer(p.accept_resend, accepted, fb)

    def inform_tracking(self, p: Params, lam: float) -> _Cost:
        esc = p.esc[Action.INFORM_TRACKING]
        mediated = _Cost(
            money=self._mediation_money(p),
            counted=self._counted_mediation(p.win),
            escalation=1.0,
        )
        # Si el paquete no llega: reembolso o reenvío, ya tarde.
        lost_options = [self.refund_full(p, self.policy.escalation_bump, True)]
        if Action.RESEND in self.allowed and self.ctx.economics.exchange_available:
            lost_options.append(self.resend(p, lam, delayed=True))
        lost = min(lost_options, key=lambda c: c.total(lam))
        r = p.resolve_inform
        waiting = _Cost(
            money=(1.0 - r) * lost.money,
            counted=(1.0 - r) * lost.counted,
            escalation=(1.0 - r) * lost.escalation,
        )
        return _Cost(
            money=esc * mediated.money + (1.0 - esc) * waiting.money,
            counted=esc * mediated.counted + (1.0 - esc) * waiting.counted,
            escalation=esc + (1.0 - esc) * waiting.escalation,
        )

    def cost(self, action: Action, p: Params, lam: float, pct: float | None) -> _Cost:
        if action is Action.REFUND_FULL:
            return self.refund_full(p)
        if action is Action.RETURN_REFUND:
            return self.return_refund(p)
        if action is Action.DEFEND:
            return self.defend(p)
        if action is Action.PARTIAL_REFUND:
            assert pct is not None
            return self.partial_refund(p, pct, lam)
        if action is Action.EXCHANGE:
            return self.exchange(p, lam)
        if action is Action.RESEND:
            return self.resend(p, lam)
        if action is Action.INFORM_TRACKING:
            return self.inform_tracking(p, lam)
        raise ValueError(action)


# ── API pública ─────────────────────────────────────────────────────────────────────────────


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _money(x: float) -> str:
    return f"${x:,.0f}"


def recommend(
    ctx: ClaimContext,
    rep_state: ReputationState,
    book: PriorBook | None = None,
    policy: Policy | None = None,
    now: datetime | None = None,
) -> Recommendation:
    book = book or PriorBook()
    policy = policy or Policy()
    actions = candidate_actions(ctx)
    model = _CostModel(ctx, rep_state, policy, actions)

    sp: ShadowPrice = shadow_price(rep_state)
    lam = sp.lam if model.regime != "none" else 0.0

    # 1) Porcentaje óptimo de reembolso parcial con medias posteriores.
    mean_p = _draw_params(book, ctx, rng=None)
    pct_star: float | None = None
    if Action.PARTIAL_REFUND in actions and ctx.partial_refund_offers:
        pct_star = min(
            sorted(ctx.partial_refund_offers),
            key=lambda x: model.partial_refund(mean_p, x, lam).total(lam),
        )

    # 2) Monte Carlo sobre la posterior con el % fijo.
    seed = policy.seed if policy.seed is not None else zlib.crc32(ctx.claim_id.encode())
    rng = random.Random(seed)
    samples: dict[Action, list[float]] = {a: [] for a in actions}
    esc_acc: dict[Action, float] = dict.fromkeys(actions, 0.0)
    wins: dict[Action, int] = dict.fromkeys(actions, 0)
    n = max(1, policy.n_draws)
    for _ in range(n):
        p = _draw_params(book, ctx, rng)
        best_a, best_c = None, float("inf")
        for a in actions:
            c = model.cost(a, p, lam, pct_star)
            tot = c.total(lam)
            samples[a].append(tot)
            esc_acc[a] += c.escalation
            if tot < best_c:
                best_a, best_c = a, tot
        wins[best_a] += 1  # type: ignore[index]

    estimates: list[ActionEstimate] = []
    for a in actions:
        vals = sorted(samples[a])
        params = {}
        if a is Action.PARTIAL_REFUND and pct_star is not None:
            params = {"pct": pct_star, "amount": round(pct_star * ctx.economics.order_amount, 2)}
        estimates.append(
            ActionEstimate(
                action=a,
                expected_cost=statistics.fmean(vals),
                cost_p05=_quantile(vals, 0.05),
                cost_p95=_quantile(vals, 0.95),
                prob_best=wins[a] / n,
                params=params,
                escalation_prob=esc_acc[a] / n,
            )
        )
    estimates.sort(key=lambda e: e.expected_cost)
    best = estimates[0]

    rationale = _rationale(ctx, rep_state, sp, lam, model.regime, best, estimates, mean_p, book)
    approval = _approval_reasons(ctx, best, policy)
    return Recommendation(
        claim_id=ctx.claim_id,
        category=ctx.category,
        action=best.action,
        params=best.params,
        expected_cost=best.expected_cost,
        prob_best=best.prob_best,
        ranking=tuple(estimates),
        reputation_lambda=lam,
        reputation_headroom=sp.headroom,
        extra_bad_days=sp.extra_bad_days,
        rationale=tuple(rationale),
        requires_approval=bool(approval),
        approval_reasons=tuple(approval),
        created_at=now or datetime.now(UTC),
    )


def _rationale(
    ctx: ClaimContext,
    rep: ReputationState,
    sp: ShadowPrice,
    lam: float,
    regime: str,
    best: ActionEstimate,
    ranking: list[ActionEstimate],
    mean_p: Params,
    book: PriorBook,
) -> list[str]:
    out: list[str] = []
    if regime == "none":
        why = "no aplica a este reclamo" if ctx.rep_status is RepStatus.NOT_APPLIES else "ya cuenta y no hay ventana"
        out.append(f"Reputación: {why} → se minimiza solo dinero.")
    else:
        out.append(
            f"Holgura de reputación: {sp.headroom} reclamo(s) antes de cruzar "
            f"{rep.claims_threshold_rate:.1%} sobre {rep.sales_window} ventas. Si este cuenta, "
            f"suma ~{sp.extra_bad_days:.1f} días en el nivel de abajo → λ = {_money(lam)}."
        )
        if regime == "incentive":
            left = ctx.hours_left_incentive
            tail = f" (quedan {left:.0f} h)" if left is not None else ""
            out.append(f"Ventana de 48 h activa{tail}: resolver bien a tiempo evita que afecte.")
    label = ACTION_LABELS[best.action]
    if best.action is Action.PARTIAL_REFUND:
        pct = best.params["pct"]
        q = mean_p.accept_partial.get(pct, 0.0)
        label += f" de {pct:.0%} ({_money(best.params['amount'])}; P(acepta) ≈ {q:.0%})"
    out.append(
        f"Recomendado: {label}. Costo esperado {_money(best.expected_cost)} "
        f"(IC90% {_money(best.cost_p05)}–{_money(best.cost_p95)}), P(mejor opción) {best.prob_best:.0%}."
    )
    if len(ranking) > 1:
        second = ranking[1]
        out.append(
            f"Siguiente: {ACTION_LABELS[second.action]} a {_money(second.expected_cost)} "
            f"(+{_money(second.expected_cost - best.expected_cost)})."
        )
    bucket = evidence_bucket(ctx.evidence_score).value
    if Action.DEFEND in {e.action for e in ranking}:
        out.append(f"Evidencia {bucket}: P(ganar mediación) ≈ {mean_p.win:.0%}.")
    if best.action is Action.DEFEND:
        n_own = book.n_obs(("esc_defend", ctx.category.value, bucket))
    else:
        n_own = book.n_obs(("esc", ctx.category.value, best.action.value))
    if n_own < 5:
        out.append(f"Basado sobre todo en priors: {n_own:.0f} caso(s) propios comparables.")
    else:
        out.append(f"Calibrado con {n_own:.0f} casos propios comparables.")
    return out


def _approval_reasons(ctx: ClaimContext, best: ActionEstimate, policy: Policy) -> list[str]:
    if policy.mode == "shadow":
        return ["modo sombra: el copiloto solo recomienda"]
    if policy.mode == "approve":
        return ["modo aprobación: toda acción requiere visto bueno"]
    reasons: list[str] = []
    if best.action not in policy.auto_actions:
        reasons.append(f"{best.action.value} no está en las acciones automáticas")
    if best.action in MONEY_ACTIONS and ctx.economics.order_amount > policy.auto_max_amount:
        reasons.append(f"monto {_money(ctx.economics.order_amount)} > tope {_money(policy.auto_max_amount)}")
    if best.prob_best < policy.min_prob_best:
        reasons.append(f"decisión cerrada: P(mejor) {best.prob_best:.0%} < {policy.min_prob_best:.0%}")
    if ctx.category is Category.OTRO:
        reasons.append("categoría no clasificada con confianza")
    return reasons

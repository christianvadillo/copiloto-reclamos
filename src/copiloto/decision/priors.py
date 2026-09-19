"""priors.py — creencias sobre cómo responden los compradores, y cómo se actualizan.

Cada probabilidad del modelo de costo es una Beta. Los priors por defecto son JUICIO EXPERTO
(no datos): sirven para arrancar un vendedor sin historial y quedan diluidos a medida que
acumula casos propios. El recomendador reporta cuántos casos propios respaldan cada número,
para que nadie confunda un prior con evidencia.

Llaves: (kind, category, qualifier)
  ("esc", cat, action)            P(el comprador pide mediación | acción ejecutada)
  ("esc_defend", cat, bucket)     P(mediación | el vendedor defiende), por fuerza de evidencia
  ("win", cat, bucket)            P(ML falla a favor del vendedor | mediación)
  ("accept_partial", cat, "0.20") P(acepta reembolso parcial de 20%)
  ("accept_exchange", cat, "")    P(acepta cambio)
  ("accept_resend", cat, "")      P(acepta reenvío / pieza faltante)
  ("recovery", cat, "")           fracción del costo recuperada de una unidad devuelta
  ("coverage", cat, "full"|"")    P(ML absorbe la pérdida si el vendedor pierde la mediación)
  ("resolve_inform", cat, "")     P(el paquete llega / el comprador espera tras informar rastreo)

Agrupamiento parcial: la celda (kind, cat, qualifier) toma prestada fuerza de las demás
categorías con el mismo (kind, qualifier), descontada por γ. Es un bayes empírico barato:
con 3 casos propios de DEFECTUOSO y 40 del resto, la celda no arranca de cero.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from copiloto.domain import Action, Category, EvidenceStrength

Key = tuple[str, str, str]

POOLED_KINDS = frozenset({"esc", "esc_defend", "accept_partial", "coverage"})


@dataclass(frozen=True)
class Beta:
    a: float
    b: float

    @classmethod
    def from_mean(cls, mean: float, strength: float) -> Beta:
        mean = min(max(mean, 1e-4), 1 - 1e-4)
        return cls(mean * strength, (1 - mean) * strength)

    @property
    def mean(self) -> float:
        return self.a / (self.a + self.b)

    @property
    def strength(self) -> float:
        return self.a + self.b

    def updated(self, successes: float, failures: float) -> Beta:
        return Beta(self.a + successes, self.b + failures)

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(self.a, self.b)


def pct_key(pct: float) -> str:
    return f"{pct:.2f}"


def logistic_acceptance(pct: float, x50: float, scale: float = 0.08) -> float:
    """Prior de aceptación de reembolso parcial: logística creciente en el porcentaje."""
    return 1.0 / (1.0 + math.exp(-(pct - x50) / scale))


# ── Priors por defecto (juicio experto; reemplazables por configuración) ───────────────────

_ESC_BY_ACTION: dict[Action, tuple[float, float]] = {
    # (media, fuerza). Conceder casi nunca termina en mediación; informar rastreo, a veces.
    Action.REFUND_FULL: (0.02, 50),
    Action.RETURN_REFUND: (0.06, 30),
    Action.PARTIAL_REFUND: (0.03, 30),
    Action.EXCHANGE: (0.06, 30),
    Action.RESEND: (0.08, 25),
    Action.INFORM_TRACKING: (0.20, 10),
}

_ESC_DEFEND: dict[EvidenceStrength, float] = {
    EvidenceStrength.FUERTE: 0.30,
    EvidenceStrength.MEDIA: 0.50,
    EvidenceStrength.DEBIL: 0.85,
}
_ESC_DEFEND_CATEGORY_FLOOR: dict[Category, float] = {
    # Arrepentimiento: el comprador tiene derecho a devolver; defender casi siempre escala.
    Category.DEVOLUCION: 0.80,
}

_WIN: dict[Category, tuple[float, float, float]] = {
    # (fuerte, media, débil): P(ML falla a favor del vendedor | mediación)
    Category.NO_RECIBIDO: (0.65, 0.35, 0.10),
    Category.DEFECTUOSO: (0.30, 0.15, 0.05),
    Category.DIFERENTE: (0.40, 0.20, 0.05),
    Category.INCOMPLETO: (0.35, 0.20, 0.05),
    Category.DEVOLUCION: (0.10, 0.05, 0.02),
    Category.CANCELACION: (0.50, 0.30, 0.10),
    Category.OTRO: (0.30, 0.20, 0.10),
}

_PARTIAL_X50: dict[Category, float] = {
    # Porcentaje al que la mitad de los compradores acepta quedarse el producto.
    Category.DEFECTUOSO: 0.30,
    Category.DIFERENTE: 0.35,
    Category.INCOMPLETO: 0.20,
    Category.DEVOLUCION: 0.25,
    Category.OTRO: 0.30,
    Category.NO_RECIBIDO: 0.90,  # no tiene sentido ofrecer % de algo que no llegó
    Category.CANCELACION: 0.90,
}

_ACCEPT_EXCHANGE: dict[Category, float] = {
    Category.DEFECTUOSO: 0.55,
    Category.DIFERENTE: 0.60,
}
_ACCEPT_RESEND: dict[Category, float] = {
    Category.NO_RECIBIDO: 0.60,
    Category.INCOMPLETO: 0.75,
}
_RECOVERY: dict[Category, float] = {
    Category.DEFECTUOSO: 0.25,
    Category.DIFERENTE: 0.85,
    Category.INCOMPLETO: 0.60,
    Category.DEVOLUCION: 0.85,
    Category.NO_RECIBIDO: 0.90,
    Category.CANCELACION: 0.95,
    Category.OTRO: 0.50,
}
_RESOLVE_INFORM: dict[Category, float] = {
    Category.NO_RECIBIDO: 0.70,
    Category.CANCELACION: 0.50,
}


def default_prior(key: Key) -> Beta:
    kind, cat_s, qual = key
    cat = Category(cat_s)
    if kind == "esc":
        mean, strength = _ESC_BY_ACTION.get(Action(qual), (0.10, 10))
        return Beta.from_mean(mean, strength)
    if kind == "esc_defend":
        mean = max(_ESC_DEFEND[EvidenceStrength(qual)], _ESC_DEFEND_CATEGORY_FLOOR.get(cat, 0.0))
        return Beta.from_mean(mean, 10)
    if kind == "win":
        fuerte, media, debil = _WIN.get(cat, (0.3, 0.2, 0.1))
        mean = {"fuerte": fuerte, "media": media, "debil": debil}[qual]
        return Beta.from_mean(mean, 8)
    if kind == "accept_partial":
        return Beta.from_mean(logistic_acceptance(float(qual), _PARTIAL_X50.get(cat, 0.30)), 6)
    if kind == "accept_exchange":
        return Beta.from_mean(_ACCEPT_EXCHANGE.get(cat, 0.40), 8)
    if kind == "accept_resend":
        return Beta.from_mean(_ACCEPT_RESEND.get(cat, 0.40), 8)
    if kind == "recovery":
        return Beta.from_mean(_RECOVERY.get(cat, 0.50), 6)
    if kind == "coverage":
        # Con Full la logística es de ML; sin Full, cubrir la pérdida del vendedor es raro.
        return Beta.from_mean(0.80 if qual == "full" else 0.10, 10)
    if kind == "resolve_inform":
        return Beta.from_mean(_RESOLVE_INFORM.get(cat, 0.40), 8)
    raise KeyError(f"llave de prior desconocida: {key}")


# ── Observaciones → conteos ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    """Lo que se aprendió de un reclamo cerrado. Campos None = no observado en este caso."""

    category: Category
    action: Action
    evidence_bucket: EvidenceStrength = EvidenceStrength.DEBIL
    pct: float | None = None  # si la acción fue reembolso parcial
    offer_accepted: bool | None = None  # parcial/cambio/reenvío aceptado
    escalated: bool | None = None  # terminó en mediación
    mediation_won: bool | None = None  # ML falló a favor del vendedor
    covered: bool | None = None  # ML absorbió la pérdida
    fulfillment_by_ml: bool = False
    recovery_fraction: float | None = None  # valor recuperado / costo, en [0,1]
    resolved_without_cost: bool | None = None  # tras informar rastreo


Counts = dict[Key, tuple[float, float]]


def counts_from_outcomes(outcomes: Iterable[Outcome]) -> Counts:
    acc: dict[Key, list[float]] = defaultdict(lambda: [0.0, 0.0])

    def add(key: Key, success: bool | float) -> None:
        s = float(success)
        acc[key][0] += s
        acc[key][1] += 1.0 - s

    for o in outcomes:
        cat = o.category.value
        if o.escalated is not None:
            if o.action is Action.DEFEND:
                add(("esc_defend", cat, o.evidence_bucket.value), o.escalated)
            elif o.offer_accepted is not False:
                # Una oferta rechazada no dice nada de P(mediación | oferta aceptada).
                add(("esc", cat, o.action.value), o.escalated)
        if o.mediation_won is not None:
            add(("win", cat, o.evidence_bucket.value), o.mediation_won)
        if o.offer_accepted is not None:
            if o.action is Action.PARTIAL_REFUND and o.pct is not None:
                add(("accept_partial", cat, pct_key(o.pct)), o.offer_accepted)
            elif o.action is Action.EXCHANGE:
                add(("accept_exchange", cat, ""), o.offer_accepted)
            elif o.action is Action.RESEND:
                add(("accept_resend", cat, ""), o.offer_accepted)
        if o.covered is not None:
            add(("coverage", cat, "full" if o.fulfillment_by_ml else ""), o.covered)
        if o.recovery_fraction is not None:
            add(("recovery", cat, ""), min(max(o.recovery_fraction, 0.0), 1.0))
        if o.resolved_without_cost is not None and o.action is Action.INFORM_TRACKING:
            add(("resolve_inform", cat, ""), o.resolved_without_cost)
    return {k: (v[0], v[1]) for k, v in acc.items()}


# ── Libro de priors + posteriores ───────────────────────────────────────────────────────────


class PriorBook:
    """Posteriores Beta por llave: prior experto + conteos propios + préstamo entre categorías."""

    def __init__(
        self,
        counts: Counts | None = None,
        overrides: dict[Key, Beta] | None = None,
        pool_gamma: float = 0.3,
    ) -> None:
        self.counts: Counts = dict(counts or {})
        self.overrides = dict(overrides or {})
        self.pool_gamma = pool_gamma
        self._pooled: Counts = self._pool(self.counts)

    @staticmethod
    def _pool(counts: Counts) -> Counts:
        pooled: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for (kind, _cat, qual), (s, f) in counts.items():
            if kind in POOLED_KINDS:
                pooled[(kind, qual)][0] += s
                pooled[(kind, qual)][1] += f
        return {(kind, "*", qual): (v[0], v[1]) for (kind, qual), v in pooled.items()}

    def prior(self, key: Key) -> Beta:
        return self.overrides.get(key) or default_prior(key)

    def n_obs(self, key: Key) -> float:
        s, f = self.counts.get(key, (0.0, 0.0))
        return s + f

    def posterior(self, key: Key) -> Beta:
        s, f = self.counts.get(key, (0.0, 0.0))
        post = self.prior(key).updated(s, f)
        kind, _cat, qual = key
        if kind in POOLED_KINDS and self.pool_gamma > 0:
            ps, pf = self._pooled.get((kind, "*", qual), (0.0, 0.0))
            post = post.updated(self.pool_gamma * (ps - s), self.pool_gamma * (pf - f))
        return post


def isotonic_increasing(values: Sequence[float], weights: Sequence[float]) -> list[float]:
    """Pool-adjacent-violators: la aceptación de un reembolso parcial no puede bajar al subir
    el porcentaje. Con pocos datos las celdas se cruzan por ruido; esto las reordena."""
    blocks: list[list[float]] = []  # [valor, peso, n]
    for v, w in zip(values, weights, strict=True):
        blocks.append([v, max(w, 1e-9), 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            wt = w1 + w2
            blocks.append([(v1 * w1 + v2 * w2) / wt, wt, n1 + n2])
    out: list[float] = []
    for v, _w, n in blocks:
        out.extend([v] * int(n))
    return out

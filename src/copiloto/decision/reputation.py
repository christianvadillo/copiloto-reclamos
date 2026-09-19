"""reputation.py — precio sombra (λ) de un reclamo que cuenta en la reputación.

Un reclamo mediado no cuesta lo mismo a todos los vendedores ni en todo momento. Lejos del
umbral del termómetro es casi gratis; pegado al umbral, uno más te baja de nivel y se lleva
visibilidad y descuento de envío durante semanas. λ tiene que depender de la HOLGURA.

Modelo: la reputación se mide sobre una ventana móvil de W días (60, o 365 con poco volumen).
Sea C(t) el número de reclamos que cuentan dentro de la ventana dentro de t días y
M = floor(τ · N) el máximo que permite el nivel actual (τ = tasa máxima, N = ventas en ventana).
Estás por debajo del nivel mientras C(t) > M. Un reclamo más suma 1 a C(t) durante W días, así
que te empuja abajo exactamente en los instantes en que C(t) = M:

    días extra en nivel bajo = ∫₀^W P(C(t) = M) dt
    λ = L · (1/W) ∫₀^W P(C(t) = M) dt + λ_base

con L = lo que dejas de ganar si pasas una ventana completa (W días) en el nivel de abajo.
Esto captura en una sola fórmula el caso "este reclamo me tumba" y el caso "ya iba a caer y
este me deja más tiempo abajo", y el hecho de que los reclamos viejos van saliendo.

    C(t) = viejos que siguen en ventana + nuevos que llegan en [0, t]
    viejos: deterministas si se conocen sus edades; si no, Binomial(n, 1 − t/W) (edades ~ U[0,W])
    nuevos: μ ~ Gamma(a0 + n, b0 + W) → predictiva Binomial Negativa(r=a, p=b/(b+t))

La Binomial Negativa integra la incertidumbre de la tasa μ en lugar de fingir que se conoce.

Supuestos explícitos: N (ventas en ventana) constante en el horizonte; el umbral aplica si hay
más de `min_sales_for_rate` ventas; L lo fija el vendedor (no es observable en la API).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from copiloto.domain import ReputationState


def negbin_pmf(k: int, r: float, p: float) -> float:
    """P(Z = k) para Z ~ NegBin(r, p) con soporte {0,1,...}: Γ(k+r)/(Γ(r) k!) p^r (1-p)^k."""
    if k < 0:
        return 0.0
    if not (0.0 < p <= 1.0) or r <= 0:
        raise ValueError(f"parámetros inválidos r={r} p={p}")
    if p == 1.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * math.log(p) + k * math.log1p(-p))


def negbin_pmf_vector(kmax: int, r: float, p: float) -> list[float]:
    """[P(Z=0), ..., P(Z=kmax)] en espacio log (p^r se va a 0 con vendedores grandes)."""
    if kmax < 0:
        return []
    if p >= 1.0:
        return [1.0] + [0.0] * kmax
    lp, lq, lg_r = math.log(p), math.log1p(-p), math.lgamma(r)
    return [math.exp(math.lgamma(k + r) - lg_r - math.lgamma(k + 1) + r * lp + k * lq) for k in range(kmax + 1)]


def binom_pmf_vector(n: int, p: float) -> list[float]:
    """[P(X=0), ..., P(X=n)] para X ~ Binomial(n, p), en espacio log."""
    if p <= 0.0:
        return [1.0] + [0.0] * n
    if p >= 1.0:
        return [0.0] * n + [1.0]
    lp, lq, lg_n = math.log(p), math.log1p(-p), math.lgamma(n + 1)
    return [math.exp(lg_n - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * lp + (n - j) * lq) for j in range(n + 1)]


@dataclass(frozen=True)
class ShadowPrice:
    lam: float  # MXN por reclamo que cuenta
    marginal_bad_fraction: float  # fracción de la ventana que este reclamo añade en nivel bajo
    bad_fraction_without: float  # fracción de la ventana en nivel bajo aunque este no cuente
    headroom: int  # M − n: cuántos más caben hoy
    max_affecting: int  # M
    window_days: float
    note: str

    @property
    def extra_bad_days(self) -> float:
        return self.marginal_bad_fraction * self.window_days


def shadow_price(state: ReputationState) -> ShadowPrice:
    w = state.window_days
    m = state.max_affecting_claims
    n = state.affecting_claims_window
    base = state.base_cost_per_claim

    if state.sales_window < max(1, state.min_sales_for_rate):
        return ShadowPrice(
            lam=base,
            marginal_bad_fraction=0.0,
            bad_fraction_without=0.0,
            headroom=state.headroom,
            max_affecting=m,
            window_days=w,
            note=f"menos de {state.min_sales_for_rate} ventas: ML no calcula tasa de reclamos",
        )

    a = state.rate_prior_shape + n
    b = state.rate_prior_rate + w
    ages = state.affecting_claim_ages_days
    steps = max(4, state.grid_steps)

    at_m = 0.0  # ∑ P(C(t) = M)
    above_m = 0.0  # ∑ P(C(t) > M): tiempo abajo aunque este reclamo no cuente
    for i in range(steps):
        t = (i + 0.5) / steps * w
        if ages is not None:
            n_old = sum(1 for age in ages if age + t < w)
            old = [0.0] * n_old + [1.0]
        else:
            old = binom_pmf_vector(n, 1.0 - t / w)
        new = negbin_pmf_vector(m, a, b / (b + t))
        # P(C = M) = Σ_j P(viejos = j) · P(nuevos = M − j)
        p_eq = sum(old[j] * new[m - j] for j in range(min(len(old) - 1, m) + 1))
        # P(C ≤ M) para el complemento
        cdf_new = []
        acc = 0.0
        for v in new:
            acc += v
            cdf_new.append(acc)
        p_le = sum(old[j] * cdf_new[m - j] for j in range(min(len(old) - 1, m) + 1))
        at_m += p_eq
        above_m += max(0.0, 1.0 - p_le)

    frac = at_m / steps
    frac_without = above_m / steps
    lam = state.level_drop_cost * frac + base
    note = (
        f"holgura {state.headroom} (M={m}, n={n}); si cuenta, suma ~{frac * w:.1f} días en nivel bajo; "
        f"sin él ya pasarías ~{frac_without * w:.1f} días abajo"
    )
    return ShadowPrice(
        lam=lam,
        marginal_bad_fraction=frac,
        bad_fraction_without=frac_without,
        headroom=state.headroom,
        max_affecting=m,
        window_days=w,
        note=note,
    )


def claims_threshold_for_level(level_id: str | None, power_seller_status: str | None) -> float:
    """Tasa máxima de reclamos para CONSERVAR el nivel actual en MLM (tabla oficial de
    reputación MX, actualizada 11/08/2025: Líderes 1%, verde 1.5%, amarillo 3%, naranja 6%).
    `4_light_green` no aparece en la tabla consultada: se asume el umbral de amarillo (3%) y
    conviene sobreescribirlo por configuración si el vendedor está en ese nivel."""
    if power_seller_status in {"silver", "gold", "platinum"}:
        return 0.01
    table = {
        "5_green": 0.015,
        "4_light_green": 0.03,
        "3_yellow": 0.03,
        "2_orange": 0.06,
        "1_red": 1.0,  # ya en el piso: no hay nivel que perder por reclamos
    }
    return table.get(level_id or "", 0.015)

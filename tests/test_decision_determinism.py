"""Determinismo del recomendador ENTRE procesos.

`recommend` siembra su RNG con crc32(claim_id): el mismo reclamo tiene que dar exactamente la
misma recomendación en cualquier proceso, no solo dentro de uno. Iterar un set/frozenset de
`Action` mientras se consumen sorteos rompe eso sin que un test en proceso lo note: `Action` es
StrEnum, el hash de `str` se aleatoriza por proceso (PYTHONHASHSEED) y queda fijo toda la vida
del proceso, incluida la sesión entera de pytest. Por eso cada semilla corre en su intérprete.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Semillas fijas (no "random") para que el test sea reproducible. Dos semillas al azar pueden
# coincidir en el orden de iteración de un set; con varias fijas, que TODAS coincidan es
# despreciable, y el propio test lo verifica con la sonda de la primera línea del script.
HASH_SEEDS = ("0", "1", "2", "3")

# Los dos reclamos juntos pasan por las 6 llaves de `Params.esc` (4 concesiones + reenvío y
# rastreo). print(float) es el repr de ida y vuelta: cualquier diferencia de bits cambia la salida.
SCRIPT = """
from copiloto.decision.recommender import Policy, recommend
from copiloto.domain import Action, Category, ClaimContext, Economics, RepStatus, ReputationState

econ = Economics(
    order_amount=1200, unit_cost=650, sale_fee=180, return_shipping_cost=110, resend_shipping_cost=110,
    handling_cost=40, mediation_labor_cost=150, exchange_available=True,
)
rep = ReputationState(sales_window=800, affecting_claims_window=11, claims_threshold_rate=0.015, level_drop_cost=60_000)
claims = [
    ClaimContext(
        "det-pdd", Category.DEFECTUOSO, econ, evidence_score=0.3,
        rep_status=RepStatus.NOT_AFFECTED, has_incentive=True, hours_left_incentive=40,
    ),
    ClaimContext("det-pnr", Category.NO_RECIBIDO, econ, evidence_score=0.5, shipment_in_transit=True),
]
print([a.value for a in frozenset(Action)])  # sonda: orden de hash de ESTE proceso
for ctx in claims:
    r = recommend(ctx, rep, policy=Policy(n_draws=300))
    print(ctx.claim_id, r.action.value, r.params)
    for e in r.ranking:
        print(" ", e.action.value, e.expected_cost, e.cost_p05, e.cost_p95, e.prob_best, e.escalation_prob)
"""


def _run(hash_seed: str) -> tuple[str, str]:
    """(sonda, recomendaciones) de un intérprete nuevo con PYTHONHASHSEED=hash_seed."""
    pythonpath = [str(SRC), os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": os.pathsep.join(filter(None, pythonpath))}
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, f"PYTHONHASHSEED={hash_seed}:\n{proc.stderr}"
    probe, _, recs = proc.stdout.partition("\n")
    return probe, recs


def test_same_claim_same_recommendation_across_processes():
    runs = {seed: _run(seed) for seed in HASH_SEEDS}
    probes = {probe for probe, _ in runs.values()}
    assert len(probes) > 1, "todas las semillas iteran los sets igual: el test no ejercitaría el hash por proceso"
    first = HASH_SEEDS[0]
    for seed in HASH_SEEDS[1:]:
        assert runs[seed][1] == runs[first][1], f"PYTHONHASHSEED={seed} ≠ PYTHONHASHSEED={first}"

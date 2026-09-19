"""Tests del núcleo de decisión: λ de reputación, priors y recomendador."""

from copiloto.decision.evidence import EvidenceFacts, score_evidence
from copiloto.decision.priors import Beta, Outcome, PriorBook, counts_from_outcomes, isotonic_increasing
from copiloto.decision.recommender import Policy, recommend
from copiloto.decision.reputation import negbin_pmf, negbin_pmf_vector, shadow_price
from copiloto.domain import (
    Action,
    Category,
    ClaimContext,
    Economics,
    EvidenceStrength,
    RepStatus,
    ReputationState,
)

ECON = Economics(
    order_amount=1200,
    unit_cost=650,
    sale_fee=180,
    return_shipping_cost=110,
    resend_shipping_cost=110,
    handling_cost=40,
    mediation_labor_cost=150,
    exchange_available=True,
)


def rep(n: int, sales: int = 800, **kw) -> ReputationState:
    return ReputationState(
        sales_window=sales,
        affecting_claims_window=n,
        claims_threshold_rate=0.015,
        level_drop_cost=60_000,
        **kw,
    )


# ── reputación ─────────────────────────────────────────────────────────────────────────────


def test_negbin_vector_matches_scalar_and_sums_to_one():
    v = negbin_pmf_vector(400, 3.5, 0.6)
    assert abs(v[7] - negbin_pmf(7, 3.5, 0.6)) < 1e-12
    assert abs(sum(v) - 1.0) < 1e-9


def test_lambda_grows_as_headroom_shrinks():
    lams = [shadow_price(rep(n)).lam for n in (0, 4, 8, 11, 12)]
    assert lams[0] < 1.0
    assert lams == sorted(lams)
    assert lams[-1] > 5_000


def test_lambda_decays_when_far_above_threshold():
    # Muy por encima del umbral un reclamo más apenas cambia el tiempo abajo.
    assert shadow_price(rep(20)).lam < shadow_price(rep(12)).lam


def test_low_volume_seller_has_no_rate_penalty():
    sp = shadow_price(rep(3, sales=8))
    assert sp.lam == 0.0 and sp.marginal_bad_fraction == 0.0


def test_big_seller_is_numerically_stable():
    sp = shadow_price(rep(1340, sales=90_000))
    assert 0.0 <= sp.marginal_bad_fraction < 0.05


def test_known_ages_path():
    ages = tuple(float(a) for a in range(0, 60, 5))
    sp = shadow_price(rep(12, affecting_claim_ages_days=ages))
    assert sp.extra_bad_days > 0


# ── priors ─────────────────────────────────────────────────────────────────────────────────


def test_isotonic_repairs_crossings():
    out = isotonic_increasing([0.2, 0.5, 0.4, 0.8], [1, 1, 1, 1])
    assert out == sorted(out)
    assert abs(out[1] - 0.45) < 1e-9


def test_counts_and_pooling():
    outcomes = [
        Outcome(Category.NO_RECIBIDO, Action.DEFEND, EvidenceStrength.FUERTE, escalated=True, mediation_won=False)
        for _ in range(10)
    ]
    counts = counts_from_outcomes(outcomes)
    assert counts[("esc_defend", "no_recibido", "fuerte")] == (10.0, 0.0)
    book = PriorBook(counts)
    post = book.posterior(("esc_defend", "no_recibido", "fuerte"))
    assert post.mean > 0.6  # prior 0.30 con fuerza 10 + 10 escalamientos
    # Préstamo entre categorías: DIFERENTE/fuerte hereda algo de la evidencia de PNR.
    pooled = book.posterior(("esc_defend", "diferente", "fuerte"))
    assert pooled.mean > PriorBook().posterior(("esc_defend", "diferente", "fuerte")).mean


def test_beta_helpers():
    b = Beta.from_mean(0.25, 8)
    assert abs(b.mean - 0.25) < 1e-9 and b.strength == 8


# ── recomendador ───────────────────────────────────────────────────────────────────────────


def test_pnr_delivered_with_strong_evidence_defends():
    ctx = ClaimContext("pnr", Category.NO_RECIBIDO, ECON, evidence_score=0.85, shipment_delivered=True)
    r = recommend(ctx, rep(2), policy=Policy(n_draws=600))
    assert r.action is Action.DEFEND
    assert r.prob_best > 0.8


def test_defective_near_threshold_concedes():
    ctx = ClaimContext(
        "pdd",
        Category.DEFECTUOSO,
        ECON,
        evidence_score=0.1,
        rep_status=RepStatus.NOT_AFFECTED,
        has_incentive=True,
        hours_left_incentive=40,
    )
    r = recommend(ctx, rep(11), policy=Policy(n_draws=600))
    assert r.action in {Action.PARTIAL_REFUND, Action.REFUND_FULL, Action.RETURN_REFUND}
    assert r.reputation_lambda > 1_000
    if r.action is Action.PARTIAL_REFUND:
        assert r.params["pct"] in ctx.partial_refund_offers


def test_already_affected_ignores_reputation():
    ctx = ClaimContext("aff", Category.DIFERENTE, ECON, evidence_score=0.5, rep_status=RepStatus.AFFECTED)
    r = recommend(ctx, rep(12), policy=Policy(n_draws=300))
    assert r.reputation_lambda == 0.0


def test_respects_api_allowed_actions():
    ctx = ClaimContext(
        "only",
        Category.DEFECTUOSO,
        ECON,
        allowed_actions=frozenset({Action.REFUND_FULL}),
    )
    r = recommend(ctx, rep(2), policy=Policy(n_draws=200))
    assert r.action is Action.REFUND_FULL and len(r.ranking) == 1


def test_auto_policy_requires_approval_above_cap():
    ctx = ClaimContext("cap", Category.DEVOLUCION, ECON, allowed_actions=frozenset({Action.RETURN_REFUND}))
    r = recommend(ctx, rep(2), policy=Policy(mode="auto", auto_max_amount=500, n_draws=200))
    assert r.requires_approval
    assert any("tope" in x for x in r.approval_reasons)


def test_history_moves_the_decision():
    ctx = ClaimContext("hist", Category.NO_RECIBIDO, ECON, evidence_score=0.85, shipment_delivered=True)
    losing = [
        Outcome(Category.NO_RECIBIDO, Action.DEFEND, EvidenceStrength.FUERTE, escalated=True, mediation_won=False)
        for _ in range(40)
    ]
    r = recommend(ctx, rep(11), book=PriorBook(counts_from_outcomes(losing)), policy=Policy(n_draws=600))
    assert r.action is not Action.DEFEND


def test_deterministic_for_same_claim():
    ctx = ClaimContext("det", Category.DEFECTUOSO, ECON, evidence_score=0.3)
    a = recommend(ctx, rep(5), policy=Policy(n_draws=300))
    b = recommend(ctx, rep(5), policy=Policy(n_draws=300))
    assert a.expected_cost == b.expected_cost and a.action == b.action


# ── evidencia ──────────────────────────────────────────────────────────────────────────────


def test_evidence_score_pnr_delivered():
    f = EvidenceFacts(shipment_status="delivered", logistic_type="drop_off", buyer_acknowledged_receipt=True)
    s = score_evidence(Category.NO_RECIBIDO, f)
    assert s.score >= 0.66 and len(s.reasons) == 3


def test_evidence_score_regret_is_weak():
    s = score_evidence(Category.DEVOLUCION, EvidenceFacts(packing_photos=3))
    assert s.score < 0.33


def test_never_recommends_defending_without_evidence():
    ctx = ClaimContext("weak", Category.DEFECTUOSO, ECON, evidence_score=0.1)
    r = recommend(ctx, rep(0), policy=Policy(n_draws=300))
    assert Action.DEFEND not in {e.action for e in r.ranking}


def test_defend_allowed_when_it_is_the_only_option():
    ctx = ClaimContext(
        "dispute", Category.DEFECTUOSO, ECON, evidence_score=0.1, allowed_actions=frozenset({Action.DEFEND})
    )
    assert recommend(ctx, rep(0), policy=Policy(n_draws=200)).action is Action.DEFEND

"""pipeline.py — orquesta UN reclamo: traer datos, clasificar, puntuar evidencia, recomendar,
redactar y persistir. Es el único lugar que conoce el orden completo del flujo descrito en
ESPECIFICACION.md §2; todo lo demás (cliente HTTP, taxonomía, decisión, redacción) son piezas
puras o de I/O aislado que este módulo combina.

`process_claim` es idempotente por diseño: calcula un hash del snapshot relevante (reclamo +
detalle + resoluciones esperadas + afecta-reputación + mensajes + envío + ofertas de parcial) y
solo genera una recomendación y un borrador nuevos si algo de eso cambió desde la última vez.
Reprocesar la misma notificación dos veces (el reintento de ML, o un `reconcile` que vuelve a
encontrar el mismo reclamo) nunca duplica trabajo — pero SIEMPRE revisa si el reclamo cerró,
para no perder el aprendizaje de un cierre aunque el snapshot ya no haya cambiado.
"""

from __future__ import annotations

import hashlib
import json
import logging
from csv import DictReader
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from copiloto.config import Settings
from copiloto.decision.evidence import ML_LOGISTICS, EvidenceFacts, score_evidence
from copiloto.decision.priors import Outcome, PriorBook, counts_from_outcomes
from copiloto.decision.recommender import Policy, recommend
from copiloto.decision.reputation import claims_threshold_for_level
from copiloto.domain import (
    Action,
    Category,
    ClaimContext,
    Economics,
    RepStatus,
    ReputationState,
    evidence_bucket,
)
from copiloto.drafting import drafter
from copiloto.drafting.llm import classify_claim_text
from copiloto.meli.client import MeliClient, MeliError, extract_claim_id
from copiloto.store import Store
from copiloto.taxonomy import classify, map_available_actions, normalize_text

logger = logging.getLogger(__name__)

_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
_RECEIPT_PHRASES = (
    "ya me llego",
    "ya lo recibi",
    "lo recibi",
    "ya llego",
    "me llego el paquete",
    "recibi el producto",
    "ya recibi",
    "llego el pedido",
)


# ── Helpers de lectura del reclamo crudo ───────────────────────────────────────────────────


def find_player(claim: dict, role: str) -> dict | None:
    for p in claim.get("players") or []:
        if p.get("role") == role:
            return p
    return None


def extract_order_id(claim: dict) -> str | None:
    # Real (verificado en vivo): `resource="order"` + `resource_id=<order_id>`, con
    # `related_entities` vacío. `related_entities` queda como respaldo.
    if claim.get("resource") == "order" and claim.get("resource_id"):
        return str(claim["resource_id"])
    for ent in claim.get("related_entities") or []:
        if ent.get("type") == "order" and ent.get("id"):
            return str(ent["id"])
    return None


def _min_action_due_date(respondent: dict | None) -> str | None:
    dates = [a.get("due_date") for a in (respondent or {}).get("available_actions", []) if a.get("due_date")]
    return min(dates) if dates else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _history_date(history: list[dict], status: str) -> datetime | None:
    for entry in history:
        if entry.get("status") == status:
            return _parse_dt(entry.get("date"))
    return None


def _buyer_text(messages: list[dict]) -> str:
    return " ".join(m.get("message", "") for m in messages if m.get("sender_role") == "complainant")


def _buyer_acknowledged_receipt(messages: list[dict]) -> bool:
    norm = normalize_text(_buyer_text(messages))
    return any(phrase in norm for phrase in _RECEIPT_PHRASES)


def _recent_buyer_texts(messages: list[dict], limit: int = 3) -> list[str]:
    texts = [m.get("message", "") for m in messages if m.get("sender_role") == "complainant" and m.get("message")]
    return texts[-limit:]


def _rep_status_from_body(body: dict) -> RepStatus:
    """`affects_reputation` es un string `affected | not_affected | not_applies` [verificado];
    `has_incentive` va aparte y el recomendador lo combina (ventana de 48 h abierta manda).
    Se aceptan booleanos por robustez: True → affected, False → not_affected."""
    affects = (body or {}).get("affects_reputation")
    if isinstance(affects, bool):
        return RepStatus.AFFECTED if affects else RepStatus.NOT_AFFECTED
    try:
        return RepStatus(str(affects)) if affects is not None else RepStatus.UNKNOWN
    except ValueError:
        return RepStatus.UNKNOWN


def _hours_left(due_date: str | None, now: datetime) -> float | None:
    dt = _parse_dt(due_date)
    if dt is None:
        return None
    return (dt - now).total_seconds() / 3600.0


# ── Evidencia fotográfica local ────────────────────────────────────────────────────────────


def list_evidence_photos(evidence_photos_dir: str | None, order_id: str | None) -> list[Path]:
    """[supuesto de layout] `<evidence_photos_dir>/<order_id>/*.{jpg,png,pdf}`: fotos que el
    vendedor ya guardó al empacar. Sin directorio configurado o sin `order_id`, no hay
    evidencia fotográfica que sumar al score ni que adjuntar al defender (ver `actions.py`)."""
    if not evidence_photos_dir or not order_id:
        return []
    folder = Path(evidence_photos_dir) / str(order_id)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _PHOTO_EXTENSIONS)


# ── Economía y reputación a partir de las respuestas de la API ─────────────────────────────


@lru_cache(maxsize=8)
def _load_sku_costs(path: str) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in DictReader(fh):
                sku = (row.get("sku") or "").strip()
                if sku and row.get("unit_cost"):
                    out[sku] = float(row["unit_cost"])
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("no se pudo leer sku_costs_path=%s: %s", path, exc)
    return out


def _build_economics(order: dict | None, settings: Settings, claim: dict | None = None) -> Economics:
    """Montos de lo RECLAMADO: si el reclamo es parcial (`quantity_type=partial`,
    `claimed_quantity` < unidades de la orden) todo se escala a esa fracción. `unit_cost` es el
    costo de reposición de lo reclamado (lo que cuesta reponer o recuperar esas unidades)."""
    items = (order or {}).get("order_items") or []
    order_amount = sum(float(it.get("unit_price", 0)) * float(it.get("quantity", 1)) for it in items)
    sale_fee = sum(float(it.get("sale_fee", 0)) * float(it.get("quantity", 1)) for it in items)
    sku_costs = _load_sku_costs(settings.sku_costs_path) if settings.sku_costs_path else {}
    unit_cost = 0.0
    for it in items:
        qty = float(it.get("quantity", 1))
        price = float(it.get("unit_price", 0))
        sku = (it.get("item") or {}).get("seller_sku")
        unit_cost += sku_costs[sku] * qty if (sku and sku in sku_costs) else price * settings.cogs_ratio * qty
    total_qty = sum(float(it.get("quantity", 1)) for it in items)
    claimed = float((claim or {}).get("claimed_quantity") or 0)
    if (claim or {}).get("quantity_type") == "partial" and 0 < claimed < total_qty:
        factor = claimed / total_qty
        order_amount, sale_fee, unit_cost = order_amount * factor, sale_fee * factor, unit_cost * factor
    return Economics(
        order_amount=order_amount,
        unit_cost=unit_cost,
        sale_fee=sale_fee,
        return_shipping_cost=settings.return_shipping_cost,
        resend_shipping_cost=settings.resend_shipping_cost,
        handling_cost=settings.handling_cost,
        mediation_labor_cost=settings.mediation_labor_cost,
        exchange_available=settings.exchange_available,
    )


def _build_reputation_state(user: dict, settings: Settings) -> ReputationState:
    rep = (user or {}).get("seller_reputation") or {}
    metrics = rep.get("metrics") or {}
    sales = metrics.get("sales") or {}
    claims_m = metrics.get("claims") or {}
    period = str(sales.get("period") or claims_m.get("period") or "60 days")
    window_days = 365.0 if "365" in period else 60.0
    threshold = claims_threshold_for_level(rep.get("level_id"), rep.get("power_seller_status"))
    return ReputationState(
        sales_window=int(sales.get("completed") or 0),
        affecting_claims_window=int(claims_m.get("value") or 0),
        claims_threshold_rate=threshold,
        level_drop_cost=settings.level_drop_cost,
        window_days=window_days,
    )


def _logistic_type(shipment: dict) -> str | None:
    """Tipo de logística del envío. La API real (x-format-new) lo anida en `logistic.type`
    (fulfillment, cross_docking, drop_off…; None en envíos `custom`); `logistic_type` plano
    queda por compatibilidad con el formato viejo."""
    return shipment.get("logistic_type") or (shipment.get("logistic") or {}).get("type")


def _expected_actions(body: Any) -> frozenset[str]:
    """Resoluciones que espera el comprador. Real: lista de
    `{player_role, user_id, expected_resolution, status}`; también acepta el formato
    `{"expected_resolutions": [{"action"}]}`."""
    items = body if isinstance(body, list) else (body or {}).get("expected_resolutions", [])
    return frozenset(
        a for e in items if isinstance(e, dict) for a in (e.get("expected_resolution") or e.get("action"),) if a
    )


def _build_evidence_facts(
    *,
    claim: dict,
    shipment: dict | None,
    shipment_history: list[dict],
    messages: list[dict],
    settings: Settings,
    order_id: str | None,
) -> EvidenceFacts:
    shipment = shipment or {}
    delivered_at = _history_date(shipment_history, "delivered")
    shipped_at = _history_date(shipment_history, "shipped")
    photos = list_evidence_photos(settings.evidence_photos_dir, order_id)
    cancellation_after_shipping = bool(shipped_at) and claim.get("type") in {"cancel_purchase", "cancel_sale"}
    return EvidenceFacts(
        shipment_status=shipment.get("status"),
        shipment_substatus=shipment.get("substatus"),
        logistic_type=_logistic_type(shipment),
        tracking_number=shipment.get("tracking_number"),
        delivered_at=delivered_at,
        shipped_at=shipped_at,
        claim_created_at=_parse_dt(claim.get("date_created")),
        packing_photos=len(photos),
        listing_attributes_match=None,
        serial_or_batch_recorded=False,
        buyer_acknowledged_receipt=_buyer_acknowledged_receipt(messages),
        cancellation_after_shipping=cancellation_after_shipping,
    )


def _evidence_summary_for_llm(facts: EvidenceFacts) -> str:
    """Sin PII: estado del envío, fechas, número de guía. Nunca direcciones, teléfonos,
    nombre completo ni datos de pago (ver `drafting/llm.py`)."""
    parts = [f"estado del envío: {facts.shipment_status or 'desconocido'}"]
    if facts.logistic_type:
        parts.append(f"tipo de logística: {facts.logistic_type}")
    if facts.shipped_at:
        parts.append(f"despachado: {facts.shipped_at.date().isoformat()}")
    if facts.delivered_at:
        parts.append(f"entregado: {facts.delivered_at.date().isoformat()}")
    if facts.tracking_number:
        parts.append(f"guía: {facts.tracking_number}")
    if facts.packing_photos:
        parts.append(f"{facts.packing_photos} foto(s) de evidencia del vendedor")
    if facts.buyer_acknowledged_receipt:
        parts.append("el comprador reconoció haberlo recibido en sus mensajes")
    return "; ".join(parts)


_GOOD_CONDITIONS = frozenset({"new", "good", "ok", "saleable", "as_new", "like_new"})
_BAD_CONDITIONS = frozenset({"damaged", "broken", "unsaleable", "different", "incomplete", "used"})

_ML_MONEY_ACTIONS = {
    "refund": Action.REFUND_FULL,
    "allow_return": Action.RETURN_REFUND,
    "allow_return_label": Action.RETURN_REFUND,
    "allow_partial_refund": Action.PARTIAL_REFUND,
    "partial_refund": Action.PARTIAL_REFUND,
}


def _infer_seller_action(actions_history: list[dict] | None) -> Action | None:
    """Qué hizo el vendedor según ML: la acción de dinero más reciente del respondent; si solo
    escribió mensajes, defendió. Sin rastro del vendedor → None (no se aprende del caso)."""
    wrote = False
    for h in reversed(actions_history or []):
        if h.get("player_role") != "respondent":
            continue
        name = h.get("action_name") or h.get("action")
        if name in _ML_MONEY_ACTIONS:
            return _ML_MONEY_ACTIONS[name]
        if name and name.startswith("send_message"):
            wrote = True
    return Action.DEFEND if wrote else None


def _snapshot_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


# ── El flujo principal ──────────────────────────────────────────────────────────────────────


def process_claim(
    *,
    store: Store,
    settings: Settings,
    meli: MeliClient,
    seller_id: str,
    claim_id: str,
    llm_client=None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)

    claim = meli.get_claim(seller_id, claim_id)
    detail = meli.get_claim_detail(seller_id, claim_id)
    reason = meli.get_claim_reason(seller_id, claim["reason_id"]) if claim.get("reason_id") else {}
    expected_body = meli.get_expected_resolutions(seller_id, claim_id)
    affects = meli.get_affects_reputation(seller_id, claim_id)
    messages = meli.get_messages(seller_id, claim_id)

    order_id = extract_order_id(claim)
    order = meli.get_order(seller_id, order_id) if order_id else None
    shipment_id = ((order or {}).get("shipping") or {}).get("id")
    shipment = meli.get_shipment(seller_id, shipment_id) if shipment_id else None
    shipment_history = meli.get_shipment_history(seller_id, shipment_id) if shipment_id else []

    respondent = find_player(claim, "respondent")
    ml_actions = [a.get("action") for a in (respondent or {}).get("available_actions", [])]
    partial_offers_body: dict = {}
    if "allow_partial_refund" in ml_actions:
        partial_offers_body = meli.get_partial_refund_offers(seller_id, claim_id)

    snapshot = {
        "claim": claim,
        "detail": detail,
        "reason": reason,
        "expected": expected_body,
        "affects": affects,
        "messages": messages,
        "order": order,
        "shipment": shipment,
        "shipment_history": shipment_history,
        "partial_offers": partial_offers_body,
    }
    snapshot_hash = _snapshot_hash(snapshot)
    existing = store.get_claim_row(claim_id)
    is_changed = existing is None or existing.get("snapshot_hash") != snapshot_hash

    buyer_text = _buyer_text(messages)
    classification = classify(
        claim.get("reason_id"), claim.get("type"), reason.get("name"), reason.get("detail"), buyer_text
    )
    if classification.needs_second_opinion and settings.llm_enabled and llm_client is not None:
        llm_classification = classify_claim_text(
            llm_client,
            settings,
            reason_name=reason.get("name"),
            reason_detail=reason.get("detail"),
            buyer_text=buyer_text,
        )
        if llm_classification is not None:
            classification = llm_classification  # el LLM gana si la regla estaba insegura y él sí respondió

    facts = _build_evidence_facts(
        claim=claim,
        shipment=shipment,
        shipment_history=shipment_history,
        messages=messages,
        settings=settings,
        order_id=order_id,
    )
    escore = score_evidence(classification.category, facts)
    economics = _build_economics(order, settings, claim)
    user = meli.get_user(seller_id, seller_id)
    rep_state = _build_reputation_state(user, settings)

    allowed_actions = map_available_actions(ml_actions, stage=claim.get("stage"))
    expected_actions = _expected_actions(expected_body)
    partial_offers = tuple(
        sorted(
            {
                float(o["percentage"]) / 100
                for o in partial_offers_body.get("available_offers", [])
                if o.get("percentage")
            }
        )
    )
    rep_status = _rep_status_from_body(affects)
    has_incentive = bool(affects.get("has_incentive"))
    hours_left = _hours_left(affects.get("due_date"), now) if has_incentive else None

    ctx_kwargs: dict = {
        "claim_id": claim_id,
        "category": classification.category,
        "economics": economics,
        "evidence_score": escore.score,
        "allowed_actions": allowed_actions,
        "shipment_in_transit": facts.in_transit,
        "shipment_delivered": facts.delivered,
        "fulfillment_by_ml": facts.logistic_type in ML_LOGISTICS,
        "days_open": max(0.0, (now - facts.claim_created_at).total_seconds() / 86400)
        if facts.claim_created_at
        else 0.0,
        "rep_status": rep_status,
        "has_incentive": has_incentive,
        "hours_left_incentive": hours_left,
        "buyer_expected": expected_actions,
    }
    if partial_offers:
        ctx_kwargs["partial_refund_offers"] = partial_offers
    ctx = ClaimContext(**ctx_kwargs)

    incentive_due_at = affects.get("due_date") if has_incentive else None
    action_due_at = _min_action_due_date(respondent)

    store.save_claim(
        claim_id=claim_id,
        seller_id=seller_id,
        status=claim.get("status"),
        stage=claim.get("stage"),
        type_=claim.get("type"),
        reason_id=claim.get("reason_id"),
        category=classification.category.value,
        confidence=classification.confidence,
        amount=economics.order_amount,
        has_incentive=has_incentive,
        affects_reputation=rep_status.value,
        incentive_due_at=incentive_due_at,
        action_due_at=action_due_at,
        evidence_score=escore.score,
        snapshot=snapshot,
        snapshot_hash=snapshot_hash,
    )
    store.add_event(
        claim_id,
        seller_id,
        "classified",
        {
            "category": classification.category.value,
            "confidence": classification.confidence,
            "source": classification.source,
        },
    )

    is_closed = claim.get("status") == "closed"
    if is_changed and not is_closed:
        outcomes = store.outcomes_for_seller(seller_id)
        book = PriorBook(counts_from_outcomes(outcomes))
        policy = Policy(
            mode=settings.mode, auto_max_amount=settings.auto_max_amount, min_prob_best=settings.min_prob_best
        )
        rec = recommend(ctx, rep_state, book=book, policy=policy, now=now)
        rec_id = store.save_recommendation(claim_id, rec)
        store.add_event(
            claim_id,
            seller_id,
            "recommended",
            {"action": rec.action.value, "expected_cost": rec.expected_cost, "prob_best": rec.prob_best},
        )

        draft_result = drafter.draft(
            category=classification.category,
            action=rec.action,
            params=rec.params,
            evidence_summary=_evidence_summary_for_llm(facts),
            buyer_messages=_recent_buyer_texts(messages),
            settings=settings,
            llm_client=llm_client,
            tracking_number=facts.tracking_number,
            eta=None,
        )
        draft_id = store.save_draft(
            claim_id,
            rec_id,
            draft_result.message,
            draft_result.source,
            draft_result.model,
            draft_result.violations,
            draft_result.seller_summary,
            draft_result.risks,
        )
        store.add_event(
            claim_id,
            seller_id,
            "drafted",
            {"source": draft_result.source, "violations": list(draft_result.violations)},
        )

        if not rec.requires_approval and not draft_result.violations:
            # Solo pasa en modo `auto` y dentro de política (ver `recommender._approval_reasons`):
            # shadow/approve SIEMPRE piden aprobación. Autoaprobar es lo que hace que `auto_max_amount`
            # y `min_prob_best` tengan un efecto real, no solo un número guardado sin usar.
            approval_id = store.create_approval(
                claim_id=claim_id,
                seller_id=seller_id,
                recommendation_id=rec_id,
                draft_id=draft_id,
                action=rec.action.value,
                params=rec.params,
                decision="approved",
                edited_message=draft_result.message,
                approved_by="auto",
            )
            store.add_event(
                claim_id, seller_id, "auto_approved", {"action": rec.action.value, "approval_id": approval_id}
            )
            store.enqueue_job("execute", {"approval_id": approval_id})
    elif not is_changed:
        store.add_event(claim_id, seller_id, "claim_unchanged", {})

    if is_closed:
        _maybe_record_outcome(
            store=store,
            meli=meli,
            seller_id=seller_id,
            claim_id=claim_id,
            claim=claim,
            evidence_score=escore.score,
            category=classification.category,
        )


def _maybe_record_outcome(
    *,
    store: Store,
    meli: MeliClient,
    seller_id: str,
    claim_id: str,
    claim: dict,
    evidence_score: float,
    category: Category,
) -> None:
    """[supuesto] Cómo se traduce el estado final del reclamo a un `Outcome` de `priors.py`:
    documentado caso por caso abajo. El upsert por `claim_id` en `store.save_outcome` hace que
    reprocesar el mismo cierre no duplique el conteo."""
    status_history = meli.get_status_history(seller_id, claim_id)
    escalated = any(h.get("stage") == "dispute" for h in status_history)
    resolution = claim.get("resolution") or {}
    benefited = set(resolution.get("benefited") or [])
    # Solo tiene sentido preguntar "¿ganó la mediación?" si de verdad hubo mediación.
    mediation_won = ("respondent" in benefited) if escalated else None

    try:
        returns_body = meli.get_returns(seller_id, claim_id) or {}
    except MeliError as exc:
        if exc.status != 404:  # 404 = el reclamo no tiene devolución (verificado en vivo)
            raise
        returns_body = {}
    review = returns_body.get("warehouse_review") or {}
    condition = str(review.get("product_condition") or "").lower()
    if condition:
        # [supuesto] vocabulario de `product_condition` no verificado: bueno → casi todo se
        # recupera, dañado/distinto → casi nada. Desconocido → no se aprende nada de esto.
        recovery_fraction = 0.9 if condition in _GOOD_CONDITIONS else 0.2 if condition in _BAD_CONDITIONS else None
    else:
        recovery_fraction = {"approved": 1.0, "rejected": 0.0}.get(review.get("result"))

    # Lo que se EJECUTÓ manda sobre lo que se recomendó: un humano pudo aprobar otra acción u
    # otro porcentaje. Sin ejecución nuestra (modo sombra: el vendedor actuó a mano) se infiere
    # de `actions-history`; atribuirle el resultado a la RECOMENDACIÓN envenenaría los priors.
    pct = None
    latest_execution = store.get_latest_execution(claim_id)
    if latest_execution is not None and latest_execution.get("status") == "done":
        action: Action | None = Action(latest_execution["action"])
        pct = (latest_execution.get("result") or {}).get("params", {}).get("pct")
    else:
        action = _infer_seller_action(meli.get_actions_history(seller_id, claim_id))
    if action is None:
        store.add_event(claim_id, seller_id, "outcome_unattributed", {"resolution": resolution.get("reason")})
        return

    reason_text = str(resolution.get("reason") or "").lower()
    offer_accepted = None
    if reason_text:
        if action is Action.PARTIAL_REFUND:
            if "partial" in reason_text:
                offer_accepted = True
            elif "refund" in reason_text or "return" in reason_text:
                offer_accepted = False  # terminó en reembolso total o devolución: rechazó el parcial
        elif action is Action.RESEND and reason_text == "seller_sent_product":
            offer_accepted = True
    resolved_without_cost = None
    if action is Action.INFORM_TRACKING and reason_text:
        resolved_without_cost = not escalated and "refund" not in reason_text and "return" not in reason_text

    fulfillment_by_ml = False
    claim_row = store.get_claim_row(claim_id)
    if claim_row:
        shipment = (claim_row.get("snapshot") or {}).get("shipment") or {}
        fulfillment_by_ml = _logistic_type(shipment) in ML_LOGISTICS

    affects_final = meli.get_affects_reputation(seller_id, claim_id)
    rep_status_final = _rep_status_from_body(affects_final)

    outcome = Outcome(
        category=category,
        action=action,
        evidence_bucket=evidence_bucket(evidence_score),
        pct=pct,
        offer_accepted=offer_accepted,
        escalated=escalated,
        mediation_won=mediation_won,
        covered=None,  # ML no expone si absorbió la pérdida del vendedor; no observado.
        fulfillment_by_ml=fulfillment_by_ml,
        recovery_fraction=recovery_fraction,
        resolved_without_cost=resolved_without_cost,
    )
    store.save_outcome(claim_id, seller_id, outcome, rep_status_final.value, resolution.get("reason"))
    store.add_event(
        claim_id,
        seller_id,
        "outcome_recorded",
        {"escalated": escalated, "mediation_won": mediation_won, "action": action.value},
    )


def record_outcome(*, store: Store, meli: MeliClient, seller_id: str, claim_id: str) -> None:
    """Job `record_outcome`: releer un reclamo y, si ya cerró, (re)registrar su `Outcome`. Es
    un atajo idempotente al mismo código que `process_claim` corre inline al detectar el
    cierre; útil para reconciliar manualmente o desde `reconcile` sin reprocesar todo."""
    claim = meli.get_claim(seller_id, claim_id)
    if claim.get("status") != "closed":
        return
    claim_row = store.get_claim_row(claim_id)
    category = Category(claim_row["category"]) if claim_row else Category.OTRO
    evidence_score = float(claim_row.get("evidence_score") or 0.0) if claim_row else 0.0
    _maybe_record_outcome(
        store=store,
        meli=meli,
        seller_id=seller_id,
        claim_id=claim_id,
        claim=claim,
        evidence_score=evidence_score,
        category=category,
    )


def reconcile_seller(*, store: Store, settings: Settings, meli: MeliClient, seller_id: str) -> int:
    """`claims/search` (status=opened) del vendedor: red de seguridad contra notificaciones
    perdidas. Encolar de más no cuesta nada (`process_claim` es idempotente). Un error de este
    vendedor (token revocado, 403) queda como evento y no detiene a los demás."""
    enqueued = 0
    try:
        search_body = meli.search_claims(
            seller_id, player_role="respondent", player_user_id=seller_id, status="opened", limit=100
        )
    except MeliError as exc:
        store.add_event(None, seller_id, "reconcile_failed", {"step": "claims_search", "error": str(exc)[:300]})
        return 0
    # La API real devuelve {"paging", "data": [...]}; "results" queda por compatibilidad.
    for claim in search_body.get("data") or search_body.get("results") or []:
        cid = str(claim.get("id") or "")  # resource_id es la ORDEN, no el reclamo
        if cid:
            store.enqueue_job("process_claim", {"seller_id": seller_id, "claim_id": cid})
            enqueued += 1
    store.add_event(None, seller_id, "reconciled", {"enqueued": enqueued})
    return enqueued


def reconcile_missed_feeds(*, store: Store, settings: Settings, meli: MeliClient) -> int:
    """`/missed_feeds` es por APP y solo lo puede leer el dueño de la app (verificado en vivo:
    401 "You must be the owner of the app" con el token de otro vendedor). Se prueba con cada
    cuenta conectada hasta que una responda; normalmente es la del desarrollador."""
    if not settings.app_id:
        return 0
    for seller in store.list_sellers():
        try:
            notes = meli.get_missed_feeds(seller.id, app_id=settings.app_id, topic="post_purchase")
        except MeliError as exc:
            if exc.status in (401, 403):
                continue  # no es el dueño de la app: probar con la siguiente cuenta
            store.add_event(None, seller.id, "reconcile_failed", {"step": "missed_feeds", "error": str(exc)[:300]})
            return 0
        enqueued = 0
        for note in notes:
            cid = extract_claim_id(note.get("resource"))
            user_id = str(note.get("user_id") or "")
            if cid and user_id:
                store.enqueue_job("process_claim", {"seller_id": user_id, "claim_id": cid})
                enqueued += 1
        store.add_event(None, seller.id, "missed_feeds_read", {"enqueued": enqueued})
        return enqueued
    store.add_event(
        None, None, "reconcile_failed", {"step": "missed_feeds", "error": "ninguna cuenta es dueña de la app"}
    )
    return 0

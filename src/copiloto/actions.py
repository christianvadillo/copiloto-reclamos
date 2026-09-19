"""actions.py — ejecuta una aprobación humana contra la API de Mercado Libre.

Entre que el vendedor aprueba y que esto corre puede pasar tiempo (el job hace cola): por eso
SIEMPRE se relee el reclamo antes de tocar nada. Si ya cerró o la acción ya no está entre las
`available_actions` del respondent, se aborta y queda un evento explicando por qué, en vez de
mandarle a Mercado Libre una acción que ya no aplica.

La idempotencia es la última línea de defensa: `executions.idempotency_key = "{claim_id}:
{action}"` es única, así que si el mismo job se reprocesa (reintento del worker, doble clic en
"aprobar" en el dashboard) la segunda ejecución no repite la llamada que mueve dinero.

En modo `shadow` esta función nunca llega a llamar a Mercado Libre, ni siquiera si alguien
logra crear una aprobación — el dashboard tampoco debería ofrecer el botón, pero esta es la
barrera que de verdad importa.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from copiloto.config import Settings
from copiloto.domain import Action
from copiloto.meli.client import MeliClient, MeliError
from copiloto.pipeline import extract_order_id, find_player, list_evidence_photos
from copiloto.store import Store
from copiloto.taxonomy import execution_plan, map_available_actions

logger = logging.getLogger(__name__)

_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
_VALID_FILENAME = re.compile(r"^[a-zA-Z0-9._-]{1,125}$")
_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".pdf": "application/pdf"}


def _upload_if_valid(meli: MeliClient, seller_id: str, claim_id: str, photo: Path) -> str | None:
    if not _VALID_FILENAME.match(photo.name):
        logger.warning("evidencia descartada por nombre inválido: %s", photo.name)
        return None
    content = photo.read_bytes()
    if len(content) > _MAX_ATTACHMENT_BYTES:
        logger.warning("evidencia descartada por tamaño (%d bytes > 5 MB): %s", len(content), photo.name)
        return None
    content_type = _CONTENT_TYPES.get(photo.suffix.lower(), "application/octet-stream")
    result = meli.upload_attachment(seller_id, claim_id, photo.name, content, content_type)
    return result.get("id")


def _step_name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def execute_approval(*, store: Store, settings: Settings, meli: MeliClient, approval_id: int) -> None:
    """Ejecuta una aprobación con idempotencia por pasos.

    La llave `{claim_id}:{action}` se reserva en `pending` ANTES de llamar a ML y cada paso
    completado (mensaje, reembolso…) queda registrado. Así:
      - un doble clic o un reintento del worker encuentra `done` → no repite nada;
      - si un paso falló con respuesta clara de ML (`failed`), una nueva aprobación reanuda desde
        ese paso sin reenviar el mensaje ya enviado;
      - si quedó `pending` (el proceso murió a mitad) o ML no confirmó (timeout/5xx en un POST),
        la ejecución queda EN DUDA y pide revisión manual: nunca se repite a ciegas algo que mueve
        dinero."""
    approval = store.get_approval(approval_id)
    if approval is None or approval.decision != "approved":
        return
    claim_id, seller_id, action = approval.claim_id, approval.seller_id, Action(approval.action)
    idempotency_key = f"{claim_id}:{action.value}"

    if settings.mode == "shadow":
        store.add_event(claim_id, seller_id, "execution_skipped_shadow", {"action": action.value})
        return

    base_result = {"params": approval.params, "approval_id": approval_id, "steps_done": []}
    existing = store.begin_execution(idempotency_key, claim_id, action.value, base_result)
    steps_done: list[str] = []
    if existing is not None:
        status, prev = existing["status"], existing["result"]
        if status == "done":
            store.add_event(claim_id, seller_id, "execution_skipped_duplicate", {"action": action.value})
            return
        if status == "pending" or prev.get("uncertain"):
            store.add_event(
                claim_id,
                seller_id,
                "execution_in_doubt",
                {"action": action.value, "steps_done": prev.get("steps_done", []), "detail": prev.get("error")},
            )
            return
        if status == "aborted":
            store.add_event(claim_id, seller_id, "execution_skipped_aborted", {"action": action.value})
            return
        steps_done = list(prev.get("steps_done", []))  # failed con respuesta clara: reanudar
        store.update_execution(idempotency_key, "pending", {**base_result, "steps_done": steps_done, "resumed": True})

    def finish(status: str, **extra) -> None:
        store.update_execution(idempotency_key, status, {**base_result, "steps_done": steps_done, **extra})

    try:
        claim = meli.get_claim(seller_id, claim_id)
    except MeliError as exc:
        finish("failed", error=str(exc), failed_step="get_claim")
        store.add_event(claim_id, seller_id, "execution_failed", {"action": action.value, "error": str(exc)})
        return
    if claim.get("status") != "opened":
        finish("aborted", reason="claim_closed", claim_status=claim.get("status"))
        store.add_event(
            claim_id, seller_id, "execution_aborted_closed", {"action": action.value, "status": claim.get("status")}
        )
        return
    respondent = find_player(claim, "respondent")
    ml_actions = [a.get("action") for a in (respondent or {}).get("available_actions", [])]
    allowed = map_available_actions(ml_actions, stage=claim.get("stage"))
    if action not in allowed and not steps_done:
        finish("aborted", reason="action_unavailable", ml_actions=ml_actions)
        store.add_event(
            claim_id, seller_id, "execution_aborted_unavailable", {"action": action.value, "ml_actions": ml_actions}
        )
        return

    pct = approval.params.get("pct")
    plan = execution_plan(action, pct=pct, stage=claim.get("stage"))
    message_text = approval.edited_message or ""
    attachments: list[str] = []
    if action is Action.DEFEND and "send-message" not in steps_done:
        order_id = extract_order_id(claim)
        for photo in list_evidence_photos(settings.evidence_photos_dir, order_id)[:5]:
            try:
                attachment_id = _upload_if_valid(meli, seller_id, claim_id, photo)
            except MeliError as exc:
                logger.warning("no se pudo subir %s: %s", photo.name, exc)
                continue
            if attachment_id:
                attachments.append(attachment_id)

    for call in plan:  # el mensaje va primero: `taxonomy.execution_plan` ya lo ordena así.
        step = _step_name(call.path)
        if step in steps_done:
            continue
        try:
            if call.sends_message:
                meli.send_message(
                    seller_id, claim_id, call.body["receiver_role"], message_text, attachments=attachments or None
                )
            elif step == "refund":
                meli.refund(seller_id, claim_id)
            elif step == "allow-return":
                meli.allow_return(seller_id, claim_id)
            elif step == "partial-refund":
                meli.partial_refund(seller_id, claim_id, call.body["percentage"])
            else:
                raise ValueError(f"MLCall no soportada por actions.py: {call.method} {call.path}")
        except MeliError as exc:
            finish("failed", error=str(exc), failed_step=step, uncertain=exc.uncertain)
            kind = "execution_in_doubt" if exc.uncertain else "execution_failed"
            store.add_event(claim_id, seller_id, kind, {"action": action.value, "step": step, "error": str(exc)})
            return
        steps_done.append(step)
        finish("pending")

    finish("done", attachments=attachments)
    store.add_event(claim_id, seller_id, "executed", {"action": action.value, "params": approval.params})

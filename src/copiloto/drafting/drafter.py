"""drafting/drafter.py — orquesta la redacción: LLM → guardrails → un reintento → plantilla.

La plantilla es la red de seguridad de todo el módulo: si el LLM está apagado, lanza una
excepción, rehúsa, o insiste en violar los guardrails incluso después de decirle por qué,
`draft()` NUNCA deja al copiloto sin un mensaje que enviar. Eso es a propósito — un reclamo sin
responder en la ventana de 48 h cuesta más que un mensaje redactado con plantilla.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from copiloto.config import Settings
from copiloto.domain import Action, Category
from copiloto.drafting import guardrails, templates
from copiloto.drafting.llm import Borrador, draft_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftResult:
    message: str
    source: str  # "llm" | "template"
    model: str | None
    violations: tuple[str, ...]  # violaciones del propio mensaje guardado (debería ser vacío)
    seller_summary: str
    risks: tuple[str, ...]


def _safe_draft(
    llm_client: Any,
    settings: Settings,
    *,
    category: Category,
    action: Action,
    params: dict,
    evidence_summary: str,
    buyer_messages: list[str],
    known_violations: list[str] | None,
) -> Borrador | None:
    """`llm.draft_message` ya captura la cadena específica→general de errores de `anthropic`;
    esta capa es la red de seguridad final para CUALQUIER otra falla del cliente inyectado
    (incluye dobles de prueba que lanzan un error genérico a propósito)."""
    try:
        return draft_message(
            llm_client,
            settings,
            category=category,
            action=action,
            params=params,
            evidence_summary=evidence_summary,
            buyer_messages=buyer_messages,
            store_name=settings.store_name,
            signature=settings.signature,
            max_chars=settings.message_max_chars,
            known_violations=known_violations,
        )
    except Exception:
        logger.exception("el cliente LLM lanzó un error inesperado redactando")
        return None


def _try_llm(
    llm_client: Any,
    settings: Settings,
    *,
    category: Category,
    action: Action,
    params: dict,
    evidence_summary: str,
    buyer_messages: list[str],
) -> DraftResult | None:
    borrador = _safe_draft(
        llm_client,
        settings,
        category=category,
        action=action,
        params=params,
        evidence_summary=evidence_summary,
        buyer_messages=buyer_messages,
        known_violations=None,
    )
    if borrador is None:
        return None
    violations = guardrails.check_message(borrador.mensaje, action, params, settings.message_max_chars)
    if not violations:
        return DraftResult(
            message=borrador.mensaje,
            source="llm",
            model=settings.llm_model,
            violations=(),
            seller_summary=borrador.resumen_vendedor,
            risks=tuple(borrador.riesgos),
        )
    logger.info("borrador del LLM violó guardrails, reintentando una vez: %s", violations)
    borrador2 = _safe_draft(
        llm_client,
        settings,
        category=category,
        action=action,
        params=params,
        evidence_summary=evidence_summary,
        buyer_messages=buyer_messages,
        known_violations=violations,
    )
    if borrador2 is None:
        return None
    violations2 = guardrails.check_message(borrador2.mensaje, action, params, settings.message_max_chars)
    if violations2:
        logger.warning("el reintento del LLM volvió a violar guardrails, cae a plantilla: %s", violations2)
        return None
    return DraftResult(
        message=borrador2.mensaje,
        source="llm",
        model=settings.llm_model,
        violations=(),
        seller_summary=borrador2.resumen_vendedor,
        risks=tuple(borrador2.riesgos),
    )


def draft(
    *,
    category: Category,
    action: Action,
    params: dict,
    evidence_summary: str,
    buyer_messages: list[str],
    settings: Settings,
    llm_client: Any | None,
    tracking_number: str | None = None,
    eta: str | None = None,
) -> DraftResult:
    if settings.llm_enabled and llm_client is not None:
        result = _try_llm(
            llm_client,
            settings,
            category=category,
            action=action,
            params=params,
            evidence_summary=evidence_summary,
            buyer_messages=buyer_messages,
        )
        if result is not None:
            return result

    text = templates.render_template(
        category,
        action,
        params=params,
        store_name=settings.store_name,
        signature=settings.signature,
        tracking_number=tracking_number,
        eta=eta,
    )
    violations = guardrails.check_message(text, action, params, settings.message_max_chars)
    if violations:
        # No debería pasar (las plantillas están diseñadas para cumplir); si pasa, mejor
        # saberlo en el borrador guardado que fingir que no hay problema.
        logger.error("la plantilla violó sus propios guardrails: %s", violations)
    return DraftResult(
        message=text,
        source="template",
        model=None,
        violations=tuple(violations),
        seller_summary="Redactado con plantilla (sin LLM).",
        risks=(),
    )

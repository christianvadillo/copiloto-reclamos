"""drafting/templates.py — plantillas fijas en español de México, sin red.

Es el "no hay LLM y aun así hay que responder": deben producir un mensaje válido (cabe en el
límite, pasa `guardrails.check_message`) para CUALQUIER combinación de categoría × acción,
incluso las que `decision.recommender.category_actions` nunca produce en la práctica — se
prueba exhaustivamente en `tests/test_drafting.py`. Por eso hay una plantilla genérica por
acción y, encima, unas pocas específicas por categoría donde el tono cambia mucho (p. ej.
"defender" en un arrepentimiento no es lo mismo que "defender" en un producto dañado).

Ninguna plantilla debe mencionar la palabra reembolso/devolución cuando la acción es DEFEND
(guardrail `_check_defend_no_promise`), ni omitir el monto/porcentaje cuando es PARTIAL_REFUND.
"""

from __future__ import annotations

from copiloto.domain import Action, Category

_GENERIC: dict[Action, str] = {
    Action.REFUND_FULL: (
        "Hola, lamentamos el inconveniente. Vamos a procesar el reembolso total de tu compra. "
        "Gracias por tu paciencia. — {firma}"
    ),
    Action.RETURN_REFUND: (
        "Hola, autorizamos la devolución. En cuanto recibamos el producto de vuelta procesamos tu "
        "reembolso. Sigue las instrucciones de Mercado Libre para el envío. — {firma}"
    ),
    Action.PARTIAL_REFUND: (
        "Hola, te ofrecemos un reembolso parcial de {monto} ({pct}) y te quedas con el producto. "
        "Quedamos atentos a tu respuesta. — {firma}"
    ),
    Action.EXCHANGE: (
        "Hola, con gusto te enviamos una unidad de reemplazo sin costo. Coordinamos la devolución "
        "de la original por la plataforma. — {firma}"
    ),
    Action.RESEND: (
        "Hola, lamentamos lo ocurrido. Te reenviamos lo faltante sin costo adicional y compartimos "
        "el número de guía en cuanto esté disponible. — {firma}"
    ),
    Action.INFORM_TRACKING: (
        "Hola, tu pedido va en camino{guia}. Fecha estimada de entrega: {eta}. Cualquier novedad te "
        "la compartimos por aquí. — {firma}"
    ),
    Action.DEFEND: (
        "Hola, revisamos tu caso con la evidencia del envío y del producto. Consideramos que la "
        "venta se realizó correctamente. Quedamos atentos si necesitas algo más. — {firma}"
    ),
}

# Solo donde el tono genérico suena raro o impreciso; para todo lo demás basta `_GENERIC`.
_OVERRIDES: dict[tuple[Category, Action], str] = {
    (Category.NO_RECIBIDO, Action.DEFEND): (
        "Hola, el rastreo de Mercado Envíos muestra el paquete como entregado{guia}. Si aún no lo "
        "localizas, te sugerimos revisar con vecinos o portería; quedamos atentos. — {firma}"
    ),
    (Category.NO_RECIBIDO, Action.INFORM_TRACKING): (
        "Hola, tu paquete sigue en camino{guia}. Fecha estimada de entrega: {eta}. Te avisamos por "
        "aquí ante cualquier novedad. — {firma}"
    ),
    (Category.NO_RECIBIDO, Action.RESEND): (
        "Hola, lamentamos la demora con tu pedido. Te reenviamos el producto sin costo adicional; "
        "en cuanto tengamos guía nueva te la compartimos. — {firma}"
    ),
    (Category.DEVOLUCION, Action.DEFEND): (
        "Hola, entendemos tu solicitud. Seguimos el proceso que indica la plataforma para este tipo "
        "de casos y te mantenemos al tanto. — {firma}"
    ),
    (Category.DEVOLUCION, Action.RETURN_REFUND): (
        "Hola, autorizamos tu devolución sin problema. Al recibir el producto en las condiciones "
        "originales procesamos el reembolso. — {firma}"
    ),
    (Category.INCOMPLETO, Action.RESEND): (
        "Hola, lamentamos que falten piezas. Te reenviamos lo faltante sin costo adicional en "
        "cuanto confirmemos el detalle. — {firma}"
    ),
    (Category.CANCELACION, Action.REFUND_FULL): (
        "Hola, procedemos a cancelar y reembolsar tu compra en su totalidad. Gracias por avisarnos a tiempo. — {firma}"
    ),
    (Category.CANCELACION, Action.DEFEND): (
        "Hola, el paquete ya fue despachado antes de tu solicitud de cancelación. Revisamos tu caso "
        "con la evidencia del envío. — {firma}"
    ),
    (Category.DIFERENTE, Action.EXCHANGE): (
        "Hola, lamentamos el error. Te enviamos la unidad correcta sin costo y coordinamos la "
        "devolución de la original. — {firma}"
    ),
    (Category.DEFECTUOSO, Action.EXCHANGE): (
        "Hola, lamentamos el desperfecto. Te enviamos una unidad nueva sin costo y coordinamos la "
        "devolución de la original. — {firma}"
    ),
}


def _tracking_fragment(tracking_number: str | None) -> str:
    return f" (guía {tracking_number})" if tracking_number else ""


def render_template(
    category: Category,
    action: Action,
    *,
    params: dict | None = None,
    store_name: str,
    signature: str,
    tracking_number: str | None = None,
    eta: str | None = None,
) -> str:
    """Siempre devuelve un mensaje. `params` trae `amount`/`pct` cuando la acción es un
    reembolso parcial (así los pone `recommender.recommend`); sin ellos usa una frase neutra,
    pero en la práctica el pipeline siempre los manda juntos."""
    params = params or {}
    template = _OVERRIDES.get((category, action), _GENERIC[action])
    amount = params.get("amount")
    pct = params.get("pct")
    return template.format(
        firma=signature or store_name,
        monto=f"${amount:,.0f}" if amount is not None else "el monto acordado",
        pct=f"{pct:.0%}" if pct is not None else "el porcentaje acordado",
        guia=_tracking_fragment(tracking_number),
        eta=eta or "en los próximos días",
    )

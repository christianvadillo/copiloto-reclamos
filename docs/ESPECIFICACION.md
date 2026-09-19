# Especificación — Copiloto de reclamos (Mercado Libre México)

Documento de diseño. Lo que está marcado **[verificado]** se confirmó contra la documentación
oficial de Mercado Libre Developers (sep-2026); **[supuesto]** es juicio experto calibrable.

## 1. Tesis del producto

El daño de un reclamo es de **tiempo**, no de razón. La API lo confirma:
`GET /post-purchase/v1/claims/{id}/affects-reputation` devuelve `has_incentive`: si el vendedor
**resuelve satisfactoriamente dentro de las primeras 48 h**, el reclamo no afecta la reputación
**[verificado]**. Fuera de esa ventana cuenta, y la reputación de MLM se calcula sobre 60 días
(365 si hubo < 40 ventas en 60 días) con umbrales de reclamos: MercadoLíder 1 %, verde 1.5 %,
amarillo 3 %, naranja 6 % **[verificado, tabla MX actualizada 11/08/2025]**.

El copiloto existe para que ninguna ventana de 48 h se pierda: recibe el webhook, clasifica,
junta evidencia, recomienda la resolución de menor costo esperado y deja la respuesta redactada
en minutos. Por defecto **no ejecuta nada** (modo sombra); ejecutar dinero exige política explícita.

## 2. Flujo

```
ML ──POST /notifications──► webhook (200 en <500 ms, dedupe, cola SQLite)
                                  │
                         worker ◄─┘  (+ reconciliador: claims/search + missed_feeds)
                           │
   fetch claim + detail + reason + expected-resolutions + affects-reputation
   + order + shipment(+history) + mensajes + fotos locales
                           │
      clasificar (taxonomy) ─► puntuar evidencia (decision/evidence)
                           │
      reputación del vendedor (GET /users/{id}) ─► λ (decision/reputation)
                           │
      recomendar (decision/recommender, Monte Carlo sobre posteriores)
                           │
      redactar (LLM Claude + guardrails; plantilla si falla)
                           │
      persistir ─► dashboard / CLI ─► aprobar ─► ejecutar (taxonomy.execution_plan)
                           │
      al cerrar: resultado → Outcome → priors (aprendizaje)
```

## 3. API de Mercado Libre usada [verificado]

Base `https://api.mercadolibre.com`, header `Authorization: Bearer <access_token>`.

| Uso | Método y path |
|---|---|
| Reclamo | `GET /post-purchase/v1/claims/{id}` |
| Resumen humano (due_date, action_responsible) | `GET /post-purchase/v1/claims/{id}/detail` |
| Motivo | `GET /post-purchase/v1/claims/reasons/{reason_id}` |
| Qué pide el comprador | `GET /post-purchase/v1/claims/{id}/expected-resolutions` |
| Ofertas de parcial | `GET /post-purchase/v1/claims/{id}/partial-refund/available-offers` → `available_offers[{amount, percentage}]` |
| ¿Afecta reputación? | `GET /post-purchase/v1/claims/{id}/affects-reputation` → `{affects_reputation, has_incentive, due_date}` |
| Mensajes | `GET /post-purchase/v1/claims/{id}/messages` |
| Enviar mensaje | `POST /post-purchase/v1/claims/{id}/actions/send-message` `{receiver_role, message, attachments?}` |
| Adjuntar archivo | `POST /post-purchase/v1/claims/{id}/attachments` (multipart `file`; JPG/PNG/PDF ≤ 5 MB; nombre ≤ 125 chars `[a-zA-Z0-9._-]`) |
| Reembolso total | `POST /post-purchase/v1/claims/{id}/expected-resolutions/refund` |
| Permitir devolución | `POST /post-purchase/v1/claims/{id}/expected-resolutions/allow-return` |
| Reembolso parcial | `POST /post-purchase/v1/claims/{id}/expected-resolutions/partial-refund` `{"percentage": 20}` (100 % no: va por `/refund`) |
| Mediación | `POST /post-purchase/v1/claims/{id}/actions/open-dispute` (no lo usamos) |
| Historial | `GET .../status-history`, `GET .../actions-history` |
| Devolución | `GET /post-purchase/v2/claims/{id}/returns` |
| Buscar | `GET /post-purchase/v1/claims/search?players.role=respondent&players.user_id={seller}&status=opened` (exige ≥ 1 filtro real; `limit` ≤ 100; `offset+limit < 10000`) |
| Orden | `GET /orders/{id}` |
| Envío | `GET /shipments/{id}` con header `x-format-new: true`; `GET /shipments/{id}/history` |
| Reputación | `GET /users/{id}` → `seller_reputation.{level_id, power_seller_status, metrics.claims.{rate,value,period}, metrics.sales.{completed,period}}` |
| Notificaciones perdidas | `GET /missed_feeds?app_id={APP_ID}&topic=claims` |

Campos del reclamo: `status` (opened/closed), `type` (mediations, return, fulfillment, ml_case,
cancel_sale, cancel_purchase, change, service), `stage` (claim, dispute, recontact, none, stale),
`reason_id` (PNR/PDD/CS…), `players[]{role, type, user_id, available_actions[]{action, due_date,
mandatory}}`, `resolution{reason, benefited[], closed_by}`, `resource`, `resource_id`,
`related_entities`. Acciones del vendedor: `send_message_to_complainant`,
`send_message_to_mediator`, `refund`, `allow_return`, `allow_return_label`,
`allow_partial_refund`, `add_shipping_evidence`, `send_attachments`, `send_tracking_number`,
`send_potential_shipping`, `return_review`, `open_dispute`.

**Notificaciones**: en "Mis aplicaciones" el tópico se llama *Post Purchase* (filtros `claims` y
`claims_actions`). Payload `{_id, resource, user_id, topic, application_id, attempts, sent,
received}` (p. ej. `"resource": "/v1/claims/1041417027"`). Responder HTTP 200 en < 500 ms; ML
reintenta ~8 veces en 1 h y **desactiva el tópico** si no respondes. No hay firma HMAC: solo
lista de IPs emisoras (`54.88.218.97, 18.215.140.160, 18.213.114.129, 18.206.34.84,
35.236.253.169, 35.245.91.34, 35.245.20.104, 35.186.182.146`). Por eso el webhook nunca confía
en el payload: solo toma el id y vuelve a pedir el recurso con el token.

**OAuth**: `https://auth.mercadolibre.com.mx/authorization?response_type=code&client_id=…&redirect_uri=…`
(+ PKCE S256 opcional). `POST /oauth/token` con `grant_type=authorization_code|refresh_token`.
`expires_in` real 10800 s (la prosa dice 6 h: usar siempre el `expires_in` recibido). El
refresh token es de **un solo uso** y rota en cada canje: persistirlo atómicamente antes de
usar el nuevo access token. Expira a los 6 meses sin uso.

**Límites**: sin número publicado global (por client id); mensajería de packs 500 rpm.
Reintentar 429/5xx con backoff exponencial y jitter.

**Usuarios de prueba**: `POST /users/test_user {"site_id":"MLM"}` (máx. 10 por cuenta, se borran
tras 60 días sin uso; publicar con título "Item de Prueba - Por favor, NO OFERTAR"). **No está
documentado que los reclamos/mediaciones funcionen con usuarios de prueba** — por eso el repo
trae un simulador de la API (`copiloto.meli.fake`) y la validación real es un experimento aparte
(`docs/PRUEBA_REAL.md`).

## 4. Núcleo de decisión (ya implementado, `src/copiloto/decision/`)

- `reputation.py`: λ = L · (1/W)∫P(C(t)=M)dt — días extra en nivel bajo que causa un reclamo más,
  con C(t) = reclamos viejos que siguen en ventana + nuevos (Binomial Negativa por incertidumbre
  en la tasa). λ es ~0 con holgura y enorme pegado al umbral.
- `priors.py`: Betas con priors de juicio experto + historial propio + préstamo parcial entre
  categorías (γ = 0.3). `Outcome` → `counts_from_outcomes` → `PriorBook`.
- `recommender.py`: costo = dinero + λ·P(cuenta). Tres regímenes (`none`, `incentive`,
  `general`). 2,000 muestras de la posterior → E[costo], IC90 %, P(mejor). Política
  shadow/approve/auto.
- `evidence.py`: score de evidencia con razones en español.
- `taxonomy.py`: clasificación por reglas (PNR/PDD/CS + palabras clave) y plan de ejecución.

## 5. Redacción (LLM)

- Modelo configurable, por defecto `claude-opus-5` vía SDK oficial `anthropic` 1.x:
  `client.beta.messages.parse(model=…, max_tokens=2000, betas=["server-side-fallback-2026-07-01"],
  fallbacks="default", output_config={"effort": "medium"}, system=…, messages=[…],
  output_format=Borrador)` → `response.parsed_output`. Revisar `stop_reason == "refusal"` antes
  de usar el contenido. Sin `temperature` (el SDK 1.x la rechaza).
- Entrada al LLM: categoría, acción recomendada y parámetros (monto/%), resumen de evidencia
  (estado de envío, fechas, número de guía), últimos mensajes del comprador, tono y firma del
  vendedor. **Nunca** direcciones, teléfonos, nombre completo ni datos de pago.
- Salida estructurada: `mensaje` (≤ 350 caracteres por defecto, configurable), `resumen_vendedor`,
  `riesgos[]`.
- Guardrails deterministas antes de guardar: longitud; sin correos, teléfonos, URLs ni
  "WhatsApp" (ML prohíbe sacar la conversación de la plataforma); coherencia con la acción (si la
  acción es parcial, el mensaje menciona el monto exacto; si es defender, no promete reembolso);
  sin amenazas de mediación. Un reintento con la lista de violaciones; si vuelve a fallar,
  plantilla.
- Plantillas por categoría × acción en español de México (siempre disponibles, sin red).

## 6. Seguridad y operación

- Tokens OAuth cifrados en reposo (Fernet, llave `COPILOTO_SECRET_KEY`); nunca en logs.
- Webhook: valida `application_id`, lista de IPs opcional, dedupe por `(topic, resource, sent)`.
- Idempotencia al ejecutar: una acción aprobada se ejecuta una sola vez (tabla `executions` con
  llave única `claim_id + action`).
- Todo cambio de estado queda en `events` (auditoría).

## 7. Validación real (fuera del repo)

Ver `docs/PRUEBA_REAL.md`: crear app en DevCenter, usuarios de prueba, compra simulada, abrir
reclamo desde la UI con el comprador de prueba, túnel HTTPS para el webhook y registro de qué
endpoints responden en cuentas de prueba. Si los reclamos no operan con usuarios de prueba, el
plan B es un vendedor real en modo sombra (sin ejecutar nada).

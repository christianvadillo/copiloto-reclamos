# Arquitectura — Copiloto de reclamos

Referencia técnica de módulos, tablas y decisiones de diseño. Para la tesis del producto y el
quickstart ver `README.md`; para los endpoints verificados de Mercado Libre ver
`ESPECIFICACION.md`.

## Módulos

```
src/copiloto/
  domain.py           vocabulario (Category, Action, Economics, ReputationState, ClaimContext,
                       Recommendation...) — sin I/O. NÚCLEO, no tocar semántica.
  taxonomy.py          ML → vocabulario del copiloto (clasificación por reglas, acciones
                       disponibles, plan de ejecución) — sin I/O. NÚCLEO.
  decision/
    reputation.py       λ (precio sombra de un reclamo que cuenta), tabla de umbrales MX. NÚCLEO.
    priors.py           Betas + históricos propios + préstamo entre categorías. NÚCLEO.
    recommender.py       Monte Carlo sobre la posterior → Recommendation. NÚCLEO.
    evidence.py           score de evidencia del vendedor. NÚCLEO.
  config.py           Settings (pydantic BaseModel, sin pydantic-settings), prefijo COPILOTO_.
  store.py            SQLite WAL: sellers, notifications, jobs, claims, recommendations,
                       drafts, approvals, executions, outcomes, events.
  meli/
    client.py            cliente HTTP del subconjunto de la API de ML que se usa.
    oauth.py              autorización + TokenProvider (refresh, rotación, needs_reauth).
    fake.py                simulador FastAPI de la misma API, para tests y `copiloto demo`.
  drafting/
    templates.py          plantillas es-MX, siempre disponibles, sin red.
    guardrails.py          reglas deterministas sobre CUALQUIER mensaje antes de guardarlo.
    llm.py                  Claude vía SDK oficial `anthropic` 1.x (`beta.messages.parse`).
    drafter.py               orquesta LLM → guardrails → 1 reintento → plantilla.
  pipeline.py          process_claim, reconcile_seller, record_outcome: el flujo completo.
  actions.py           ejecuta una aprobación (idempotente, releyendo el reclamo primero).
  worker.py            bucle que toma jobs de `store.jobs` y despacha.
  app.py               FastAPI: webhook, OAuth, dashboard Jinja2, API JSON.
  web/templates/         base.html, list.html, detail.html.
  cli.py               entrypoint `copiloto` (serve, worker, reconcile-once, init-db,
                       auth-url, stats, demo).
```

## Por qué está partido así

El núcleo (`domain.py`, `taxonomy.py`, `decision/*`) no hace I/O y no sabe que existe Mercado
Libre, HTTP ni SQLite: solo recibe un `ClaimContext` ya traducido y devuelve una
`Recommendation`. Eso es lo que lo hace 100 % testeable sin red y lo que permite que el resto
del sistema (webhook, worker, dashboard) cambie sin arriesgar la lógica de dinero.

`taxonomy.py` es la única frontera entre el vocabulario de Mercado Libre (`reason_id`,
`available_actions`, `stage`...) y el vocabulario del copiloto (`Category`, `Action`). Nada más
en el repo debería ver un string crudo de ML — si algún día cambia un nombre de acción en la
API, el cambio se hace en un solo archivo.

## Flujo de un reclamo

1. `POST /notifications` (`app.py`) valida `application_id` (y opcionalmente la IP emisora),
   deduplica por `(topic, resource, sent)` y encola `process_claim`. Nunca llama a Mercado
   Libre: debe responder en milisegundos porque ML reintenta ~8 veces en 1 h y **desactiva el
   tópico** si no contestas rápido.
2. `worker.py` toma el job y corre `pipeline.process_claim`, que:
   - trae claim + detail + reason + expected-resolutions + affects-reputation + mensajes +
     orden + envío (+historial) + ofertas de parcial (si `allow_partial_refund` está
     disponible);
   - calcula un hash del snapshot relevante: si no cambió desde la última vez, no genera una
     recomendación/borrador nuevos (pero SIEMPRE revisa si el reclamo cerró, para no perder el
     aprendizaje de un cierre aunque el snapshot no haya cambiado);
   - clasifica con `taxonomy.classify` (segunda opinión del LLM solo si la regla está insegura
     y el LLM está habilitado; si el LLM falla o rehúsa, gana la regla);
   - construye `EvidenceFacts` → `score_evidence`; `Economics` desde la orden + config;
     `ReputationState` desde `GET /users/{seller}`;
   - arma el `ClaimContext` y llama a `decision.recommender.recommend` con el `PriorBook` del
     vendedor (`counts_from_outcomes` sobre sus `outcomes` cerrados);
   - redacta con `drafting.drafter.draft` (LLM → guardrails → reintento → plantilla);
   - persiste `claims`, `recommendations`, `drafts` + eventos de auditoría;
   - **si `recommendation.requires_approval` es `False`** (solo posible en modo `auto` y dentro
     de política — ver `decision/recommender.py:_approval_reasons`), crea una aprobación con
     `approved_by="auto"` y encola `execute`. Sin este paso el modo `auto` no auto-ejecutaría
     nada y `auto_max_amount`/`min_prob_best` serían configuración sin efecto real;
   - si el reclamo está cerrado, registra un `Outcome` (ver más abajo).
3. El vendedor aprueba/edita/rechaza en el dashboard (`GET/POST /claims/{id}`). Aprobar corre
   `guardrails.check_message` sobre el texto FINAL (editado o no) antes de encolar `execute`; si
   viola algo, no se aprueba y se muestra por qué.
4. `actions.execute_approval` relee el reclamo (puede haber pasado tiempo desde la aprobación),
   verifica que siga abierto y que la acción siga entre las `available_actions`, y ejecuta
   `taxonomy.execution_plan` en orden (mensaje primero, adjuntando evidencia si la acción es
   `DEFEND` y hay fotos configuradas). Idempotente por `executions.idempotency_key =
   "{claim_id}:{action}"`.
5. `reconcile_seller` (cron cada `reconcile_interval_min`, o `copiloto reconcile-once`) busca
   `claims/search?status=opened` + `missed_feeds` y encola `process_claim` para lo que
   encuentre — red de seguridad contra notificaciones perdidas; encolar de más no cuesta nada
   porque `process_claim` es idempotente.

## El `Outcome` usa lo EJECUTADO, no lo recomendado

`pipeline._maybe_record_outcome` mira primero `Store.get_latest_execution(claim_id)`: si hubo
una ejecución, la acción y el porcentaje del `Outcome` son los que de verdad se mandaron a
Mercado Libre (un humano pudo aprobar algo distinto a lo recomendado, o editar el porcentaje).
Solo si no hubo ejecución (modo shadow, o el reclamo se cerró sin que el copiloto hiciera nada)
cae a la última recomendación como mejor estimado de qué pasó. Esto importa porque el
`PriorBook` aprende de `outcomes`: si contara la recomendación en vez de la ejecución, un
vendedor que sistemáticamente edita antes de aprobar entrenaría al modelo con datos falsos.

## Tablas (`store.py`)

| Tabla | Qué guarda | Llave relevante |
|---|---|---|
| `sellers` | tokens OAuth cifrados (Fernet), `needs_reauth` | `id` (user_id de ML) |
| `notifications` | payload de cada webhook recibido | `dedupe_key` UNIQUE = `topic\|resource\|sent` |
| `jobs` | cola: `process_claim`, `execute`, `reconcile`, `record_outcome` | toma atómica vía `BEGIN IMMEDIATE` |
| `claims` | snapshot + campos indexables (status, categoría, monto, vencimientos) | `claim_id` |
| `recommendations` | ranking completo + rationale + λ | `claim_id` (1:N, se usa la última) |
| `drafts` | mensaje + fuente (llm/template) + violaciones | `claim_id` (1:N) |
| `approvals` | decisión humana o `auto`, mensaje editado | `claim_id` (1:N) |
| `executions` | qué se ejecutó de verdad | `idempotency_key` UNIQUE = `claim_id:action` |
| `outcomes` | resultado final para el `PriorBook` | `claim_id` UNIQUE (upsert) |
| `events` | auditoría de todo cambio de estado | — |

Migraciones: `PRAGMA user_version` + bloques de DDL `CREATE TABLE IF NOT EXISTS` — aplicarlas de
más no rompe nada (se prueba en `tests/test_store.py::test_migrations_are_idempotent`).

## Jobs y colas

Una sola tabla SQLite hace de cola (`jobs`), tomada atómicamente con `BEGIN IMMEDIATE` para que
el webhook y el worker (procesos separados) nunca choquen. Reintentos con backoff exponencial
(tope 5 min); agotados los intentos, el job queda `dead` en vez de reintentarse para siempre.
`worker.py` despacha cuatro tipos: `process_claim`, `execute`, `reconcile`, `record_outcome`.

## Supuestos documentados (no verificados contra la API real)

Marcados `[supuesto]` en el código fuente, con la razón al lado:

- Forma exacta del cuerpo de `expected-resolutions`, `claims/reasons/{id}` y `returns` v2 (los
  PATHS y los campos de `affects-reputation`/`partial-refund/available-offers` sí están
  verificados en `ESPECIFICACION.md`).
- Cómo se combinan `affects_reputation` y `has_incentive` en un solo `RepStatus`
  (`pipeline._rep_status_from_body`).
- Layout de evidencia fotográfica local: `<evidence_photos_dir>/<order_id>/*.{jpg,png,pdf}`.
- Heurísticas de `pipeline._maybe_record_outcome` (cuándo se considera que el comprador aceptó
  una oferta, cuándo se resolvió "sin costo" tras informar rastreo).

Ninguno de estos supuestos afecta al núcleo de decisión (que no los conoce): son puntos donde
`pipeline.py` traduce respuestas de la API real a los tipos del núcleo, y están aislados ahí a
propósito para poder corregirlos con evidencia de `docs/PRUEBA_REAL.md` sin tocar la lógica de
costo.

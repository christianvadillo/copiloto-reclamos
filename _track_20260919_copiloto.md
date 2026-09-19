# PLAN — Copiloto de reclamos (Mercado Libre MX)

Construir desde cero un servicio que reciba el webhook de reclamos, clasifique, junte evidencia,
recomiende la resolución de menor costo esperado y redacte la respuesta con Claude, con
aprobación humana por defecto. Repo local + remoto privado en GitHub.

## ANÁLISIS

- El valor está en el TIEMPO: `affects-reputation.has_incentive` confirma que resolver bien en
  las primeras 48 h evita que el reclamo cuente. El producto es un reloj con criterio.
- La decisión correcta depende de la holgura de reputación: el mismo reclamo defectuoso se
  defiende con holgura y se concede pegado al umbral. Eso pide un precio sombra λ dependiente
  del estado, no una regla fija.
- Todo lo que mueve dinero debe ser opt-in: modo sombra por defecto.

## INVESTIGACIÓN

- API ML verificada (agente de investigación, sep-2026): tópico "Post Purchase" (`claims`,
  `claims_actions`), 200 en < 500 ms, sin HMAC (lista de 8 IPs), endpoints `/post-purchase/v1/...`
  vigentes (`expected-resolutions/refund|allow-return|partial-refund`, `actions/send-message`,
  `affects-reputation`), OAuth con refresh de un solo uso y `expires_in` 10800.
- Umbrales MX (11/08/2025): reclamos Líder 1 %, verde 1.5 %, amarillo 3 %, naranja 6 %; ventana
  60 días o 365 si < 40 ventas.
- **Hueco**: no está documentado que reclamos funcionen con usuarios de prueba → simulador de
  API en el repo + runbook de validación real como experimento separado.
- SDK `anthropic` 1.7: `client.beta.messages.parse(..., fallbacks="default",
  betas=["server-side-fallback-2026-07-01"], output_format=Modelo)`; sin temperature.

## CAMBIOS

- [x] Núcleo de decisión (Opus): `domain.py`, `taxonomy.py`, `decision/{reputation,priors,recommender,evidence}.py` + 31 tests
- [x] λ por tiempo esperado en nivel bajo: L·(1/W)∫P(C(t)=M)dt con viejos Binomial + nuevos NegBin
- [x] Especificación `docs/ESPECIFICACION.md`
- [x] Servicio (worker Sonnet): config, cliente ML + OAuth, store, webhook, worker, pipeline, redacción LLM + guardrails, acciones, dashboard, API falsa, CLI `demo`, tests, docs, CI
- [x] Revisión del hilo principal: núcleo intacto (31 tests originales sin tocar), 115 tests nuevos
      del servicio (146 total), ruff check + format limpios, `copiloto demo` verde de punta a punta
- [x] Determinismo entre procesos del Monte Carlo (`decision/recommender.py`): `_draw_params`
      iteraba `CONCESSIONS | {INFORM_TRACKING}` (frozenset de StrEnum → orden según PYTHONHASHSEED)
      consumiendo el RNG compartido; ahora itera la tupla `_ESC_ACTIONS` en el orden de declaración
      de `Action`. Solo cambia qué sorteo cae en qué llave. Test: `tests/test_decision_determinism.py`
- [x] Revisión del hilo principal (Opus) — ver bitácora: 9 correcciones de correctitud/seguridad
- [x] Commit inicial + repo remoto privado + push
- [x] Vault: nota en `diario-global/` (convención de proyectos no-bot, como Metrónomo y Caso Abierto) + memoria

## VALIDACIÓN

- Núcleo: 31 tests verdes; escenarios de humo coherentes (PNR entregado + evidencia fuerte →
  defender; defectuoso pegado al umbral con ventana 48 h → reembolso parcial; ya afectado →
  solo dinero, P(mejor) baja → revisión).
- Servicio: 146 tests verdes en ~3.2 s (`ruff check`/`format --check` limpios sobre
  `src`+`tests`+`scripts`). Cubre: webhook (200 rápido, dedupe, app_id ajeno 403, tópicos
  ignorados), pipeline E2E contra `meli.fake` para los 7 escenarios de la especificación,
  aprobación→ejecución (refund cierra, parcial queda pendiente), doble aprobación idempotente,
  modo sombra nunca ejecuta, rotación de refresh token persistida antes de devolver el access
  token, guardrails caso por caso, las 49 combinaciones categoría×acción de plantillas dentro
  del límite, el drafter cae a plantilla cuando el LLM falso lanza error/rehúsa/insiste en
  violar guardrails, reclamo cerrado → Outcome (con la acción REALMENTE ejecutada, no solo la
  recomendada) → cuenta en el `PriorBook`, migraciones idempotentes, dashboard (lista/detalle/
  aprobar/rechazar + basic auth). `copiloto demo` corre offline en ~1.5-1.6 s end-to-end.
- Determinismo entre procesos: `test_decision_determinism.py` corre el recomendador en 4
  intérpretes (PYTHONHASHSEED 0-3) y exige floats idénticos; con el código anterior falla (seed
  1 ≠ seed 0). El código nuevo con el orden viejo inyectado reproduce byte a byte las 8 salidas
  pre-fix del demo (seeds 0-7) → el cambio es solo de orden. Demo post-fix: 18 corridas (16 seeds
  fijos + 2 `random`) idénticas salvo la ruta de la BD temporal.

## BITÁCORA

- 2026-09-19 — Investigación API ML (reclamos) + núcleo de decisión escrito y probado. El primer
  λ (probabilidad de cruzar) subestimaba el daño cuando cruzar ya era casi seguro; se reemplazó
  por "días extra en nivel bajo" integrando sobre la ventana (curva correcta: sube hasta H=0 y
  decae muy por encima del umbral). Desbordes con vendedores grandes resueltos en espacio log.
- 2026-09-19 — Construido el servicio completo alrededor del núcleo intacto: `config.py` (Settings
  sin pydantic-settings), `store.py` (SQLite WAL + migraciones `PRAGMA user_version` + cola de
  jobs con `BEGIN IMMEDIATE`), `meli/{client,oauth,fake}.py`, `drafting/{templates,guardrails,
  llm,drafter}.py`, `pipeline.py`, `actions.py`, `worker.py`, `app.py` + dashboard Jinja2,
  `cli.py` con `demo` offline, `scripts/crear_usuarios_prueba.py`. Dos huecos reales que apareció
  construir (no estaban explícitos en la letra de la especificación) y se cerraron: (1) modo
  `auto` no auto-aprobaba nada — `process_claim` ahora crea la aprobación y encola `execute`
  cuando `recommendation.requires_approval` es `False`; (2) el `Outcome` de un reclamo cerrado
  tomaba la acción de la última RECOMENDACIÓN en vez de la EJECUCIÓN real — se agregó
  `Store.get_latest_execution` y `pipeline._maybe_record_outcome` ahora prioriza lo ejecutado.
- 2026-09-19 — QA del `app.py` recién escrito encontró un tercer bug real, más sutil: el basic
  auth del dashboard (`COPILOTO_DASHBOARD_USER`/`PASSWORD`) quedaba SIEMPRE en 401 incluso con
  credenciales correctas. Causa: `require_dashboard_auth` está definida DENTRO de `create_app`
  (closure) y usaba `credentials: Annotated[HTTPBasicCredentials | None, Depends(security)] =
  None`; con `from __future__ import annotations` (PEP 563) toda anotación se vuelve string y
  FastAPI la resuelve con `typing.get_type_hints()` usando SOLO los globals del módulo — nunca
  los locals de la función envolvente — así que no encuentra `security` (variable local de
  `create_app`), y en vez de fallar ruidosamente reinterpreta `credentials` como query param
  normal: la auth queda rota en silencio, sin ningún error en el arranque ni en los logs. Se
  corrigió usando el estilo viejo `credentials: HTTPBasicCredentials | None = Depends(security)`
  (el `Depends(...)` se evalúa como valor por defecto de inmediato, no se stringifica, así que
  sí ve el closure). Se agregó `tests/test_dashboard.py` con una prueba de regresión explícita
  (auth correcta → 200, antes daba 401) más cobertura de lista/detalle/aprobar/rechazar. Esto es
  genuinamente fácil de reintroducir sin darse cuenta (el patrón `Annotated[X, Depends(y)]` es
  el que recomienda la documentación de FastAPI) — el comentario en el código explica por qué
  aquí específicamente NO se usa. 146 tests totales (115 nuevos + 31 del núcleo intacto), ruff
  limpio, `copiloto demo` verde por `copiloto demo` y por `python -m copiloto.cli demo`.
- Hallazgo fuera de alcance, NO corregido (el núcleo `decision/` está fuera de mandato): el
  Monte Carlo de `recommend()` se siembra con `zlib.crc32(claim_id)` buscando reproducibilidad,
  pero `_draw_params` itera `frozenset` de `Action` (p. ej. `CONCESSIONS | {INFORM_TRACKING}`)
  cuyo orden de iteración depende del hash-seed de cada proceso de Python — confirmado con
  `PYTHONHASHSEED=0` fijo (dos corridas de `copiloto demo` dan salida idéntica) vs. sin fijar
  (los montos de E[costo]/P(mejor) varían ~1-2% entre reinicios, mismo claim_id). No es un bug
  de exactitud estadística (cada corrida sigue siendo una muestra válida de la posterior), solo
  de reproducibilidad byte a byte entre reinicios del proceso. Se dejó una tarea en cola
  (`spawn_task`, título "Fix cross-process nondeterminism in recommender Monte Carlo") con la
  causa raíz y el fix propuesto, sin tocar `decision/recommender.py` en esta ronda.
- 2026-09-19 09:55 — Resuelto el hallazgo anterior. Único sitio real: la comprehension de
  `Params.esc` en `_draw_params`. Como `betavariate` consume un número variable de uniformes
  (rechazo en `gammavariate`), una permutación desplaza TODO el flujo posterior, no solo las 6
  llaves de `esc`. Auditado el resto: `candidate_actions` ya ordena; `_CostModel.allowed`,
  `MONEY_ACTIONS`, `auto_actions` y `POOLED_KINDS` (frozenset de `str`) solo se usan para
  pertenencia; `_pool` itera un dict (orden de inserción); las ofertas parciales van `sorted`.
  `priors.py` sin cambios. Fórmulas, priors, regímenes y clasificación intactos; los 31 tests del
  núcleo sin tocar y verdes. Suite: 149 verdes (146 + este test + 2 de `test_actions.py` que otra
  sesión agregó en paralelo); ruff check + format limpios. README: se quitó la advertencia de
  "los montos pueden variar entre ejecuciones" y el extracto del demo es ahora la salida exacta.
- 2026-09-19 — Revisión del hilo principal sobre lo construido. Corregido:
  1. `affects_reputation` es string (`affected|not_affected|not_applies`); se leía como booleano y
     `"not_affected"` (truthy) caía en AFFECTED → régimen "none" → λ=0: la reputación se ignoraba.
  2. POST de dinero ya no se reintenta a ciegas: solo 429 o error de CONEXIÓN; timeout de lectura
     o 5xx → `MeliError(uncertain=True)`. Antes un timeout tras un reembolso parcial lo repetía.
  3. Ejecución idempotente POR PASOS: la llave se reserva en `pending` antes de llamar a ML y cada
     paso queda registrado; `failed` con respuesta clara reanuda sin reenviar el mensaje, `pending`
     o `uncertain` quedan EN DUDA para revisión manual (2 tests nuevos).
  4. Guardrail de parcial exige EL porcentaje/monto que se va a ejecutar (antes aceptaba cualquier
     número: "30 %" pasaba con una ejecución de 20 %).
  5. PII: los mensajes del comprador se redactan (correo, teléfono, URL, tarjeta) antes del LLM y
     van delimitados como datos (defensa contra instrucciones inyectadas); categoría del LLM como
     `Literal`.
  6. El copiloto ya no recomienda "defender" con evidencia débil (salvo que sea lo único que deja
     la API): la demo recomendaba defender un defectuoso sin evidencia porque 30 % de desistimiento
     lo hacía barato en dinero. Prior de escalamiento con evidencia débil 0.70 → 0.85.
  7. Reclamo cerrado ya no genera recomendación ni borrador (ni auto-ejecución); modo auto exige
     borrador sin violaciones.
  8. Economía de lo RECLAMADO (`quantity_type=partial` + `claimed_quantity`), no de toda la orden.
  9. Aprendizaje: en modo sombra la acción se infiere de `actions-history` en vez de atribuirle el
     resultado a la recomendación (envenenaba los priors); aceptación de parcial y recuperación
     solo se registran cuando se pueden inferir. XSS en `/oauth/callback` escapado.
  La tarea de no-determinismo del Monte Carlo la resolvió la sesión aparte (`_ESC_ACTIONS` +
  `tests/test_decision_determinism.py`). Total: 153 tests verdes, ruff limpio, demo coherente.
- 2026-09-19 — Sandbox interactivo (`copiloto sandbox`): panel real + worker + consola donde se
  hace de comprador/ML; verificado en el navegador el ciclo aprobar → oferta 30 % → acepta →
  Outcome registrado. La API falsa ahora sigue la regla de 48 h en `affects-reputation`.
- 2026-09-19 — Calculadora pública `/calculadora` (primer escalón del embudo comercial, sin
  acceso a la cuenta): holgura, costo mensual de bajar de nivel y λ por reclamo con la curva.
  Modelo portado a JS y verificado contra Python (n=8/11/12/13 → 1,753/6,808/9,696/8,027 con
  L=$60k; vendedor grande idéntico). Publicada también como Artifact privado.
- 2026-09-19 — **Prueba real de punta a punta con usuarios de prueba: funciona.** Reclamo
  5579999933 (PDD9947, $500) → defectuoso 85 % → reembolso total, borrador de plantilla, modo
  sombra. Seis diferencias entre la API real y el simulador, todas corregidas y reflejadas en el
  fake: tópico `post_purchase`, search en `data`, orden en `resource_id`, expected-resolutions en
  lista, returns 404 y `logistic.type` anidado; `/missed_feeds` da 401 a quien no es dueño. La
  orden no se leía, así que el monto salía en $0; ahora sale en $500. 163 tests.
- 2026-09-19 — **Ciclo completo en vivo.** Aprobé y ejecuté el reembolso del reclamo de prueba
  5579999933 (candado: solo vendedor TESTUSER, modo approve solo para esa corrida). Pasos:
  send-message → refund, ejecución `done`; ML lo cerró en ~1 s (`payment_refunded`,
  closed_by respondent, benefited complainant) y el copiloto registró el resultado en
  `outcomes`. Bug encontrado al reiniciar el servidor a mitad de un job: los jobs en `running`
  nunca se retomaban → lease de 10 min en `claim_job`. 165 tests.
- 2026-09-19 — **Decisión: pausa comercial.** El usuario no quiere invertir tiempo en entrevistas
  con vendedores; sin eso el camino SaaS no tiene forma de validarse. El repo se reorienta a
  pieza de portafolio para su búsqueda de trabajo: README con "qué demuestra", capturas reales
  (panel, detalle con ranking, el reclamo real cerrado), diagrama Mermaid del flujo, la lección
  de las 6 diferencias de contrato, y `docs/GUION_ENTREVISTA.md` (3 min + preguntas frecuentes
  + qué archivos enseñar). No se hace hosting ni onboarding: el código queda funcionando.

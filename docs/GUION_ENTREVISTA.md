# Guion de 3 minutos — "cuéntame de un proyecto tuyo"

Notas para contar este proyecto en una entrevista. No es para leerse en voz alta: son los cinco
pasos de la historia y las respuestas a lo que normalmente se pregunta después.

---

## El guion (≈3 min)

**1. El problema, en una frase (20 s).**
Un vendedor de Mercado Libre que recibe un reclamo tiene 48 horas para resolverlo bien; si lo
hace, el reclamo no cuenta contra su reputación. Fuera de esa ventana, sí. Y bajar de nivel de
reputación cuesta dinero medible: se pierde el descuento en envíos, que con mil envíos al mes
son del orden de diez mil pesos mensuales. Así que el problema no es "qué hago con este
reclamo", es "qué hago con este reclamo **hoy**".

**2. Por qué no es un problema de clasificación (40 s).**
Lo obvio sería entrenar algo que diga "reembolsa" o "defiende". Pero la decisión correcta
depende de qué tan cerca está el vendedor de su umbral. Con holgura de sobra, defender un
reclamo dudoso sale barato. Pegado al umbral, el mismo reclamo conviene concederlo de
inmediato, aunque cueste más dinero directo. Una regla fija pierde dinero de un lado o del
otro. Entonces lo modelé como una decisión bajo incertidumbre: cada acción tiene un costo
esperado en pesos, y la reputación entra como precio sombra.

**3. La parte de la que estoy más contento (45 s).**
Ese precio sombra. La reputación no aparece en ninguna API como un costo, hay que construirlo.
Lo definí como los días extra que el vendedor pasaría en el nivel de abajo si este reclamo
cuenta, integrando la probabilidad de estar cruzado a lo largo de la ventana de 60 días, con
los reclamos nuevos que entran modelados como un proceso de conteo y los viejos que salen como
un binomial. Mi primera versión medía "probabilidad de cruzar el umbral", y la tiré: cuando
cruzar ya era casi seguro, daba casi cero justo cuando más importaba. La versión de días en el
nivel de abajo se comporta bien en los dos extremos.

**4. Que funciona de verdad (40 s).**
Lo conecté a la API real: OAuth con PKCE, webhook, colas, ejecución con idempotencia por pasos
para que nada que mueva dinero se repita. Procesó un reclamo real de punta a punta: lo
clasificó, recomendó reembolso total, redactó la respuesta, la envió, ejecutó el reembolso,
Mercado Libre cerró el caso, y el resultado quedó guardado para actualizar sus propias
probabilidades. Por defecto corre en modo sombra: solo recomienda, no toca nada.

**5. La lección (35 s).**
Tenía 159 pruebas en verde y el simulador de la API pasaba todas. Al conectarlo a la API real
aparecieron seis diferencias de contrato. La peor: la orden venía en un campo distinto del que
yo suponía, así que el monto del reclamo salía en cero pesos — sin excepción, sin prueba roja, y
con una recomendación que se veía perfectamente razonable. La causa de fondo es que yo había
escrito el simulador con los mismos supuestos que el código: validaba que fuera consistente
conmigo mismo, no que el contrato fuera cierto. Desde entonces, los formatos que vienen de un
tercero los fijo contra respuestas reales capturadas, no contra lo que yo creo que devuelve.

---

## Lo que suelen preguntar después

**"¿Por qué bayesiano y no un modelo entrenado?"**
Porque no había datos. Un vendedor mediano tiene decenas de reclamos al año, no miles. Con ese
tamaño de muestra un modelo entrenado memoriza. El enfoque bayesiano me deja arrancar con priors
razonados y que se diluyan conforme entran casos reales, y además la recomendación viene con un
intervalo, no con un número seco. Cuando ninguna opción domina, el sistema lo dice y manda a
revisión humana en vez de fingir confianza.

**"¿Cómo evitas que el LLM invente cosas?"**
El LLM solo escribe el texto. Los montos y los porcentajes los fija el código, y un guardrail
determinista rechaza el borrador si aparece una cifra distinta de la aprobada. Los datos
personales del comprador se quitan antes de mandar nada al modelo, y el contenido del comprador
va delimitado como dato, no como instrucción. Si el LLM falla o se tarda, cae a plantillas; el
sistema nunca se queda sin respuesta por depender de él.

**"¿Qué pasa si se cae a la mitad de un reembolso?"**
Cada ejecución reserva una llave antes de llamar a la API y registra los pasos completados. Si
un paso quedó sin confirmar — un timeout, un 5xx — el caso se marca "en duda" y pide revisión
manual, en vez de reintentar a ciegas algo que mueve dinero. Un reintento que sí sabe dónde se
quedó reanuda sin reenviar el mensaje que ya salió. Eso lo encontré en revisión: la primera
versión reintentaba el POST como si fuera idempotente, y no lo es.

**"¿Y si el proceso se muere con un trabajo a medias?"**
Me pasó en vivo: reinicié el servidor a mitad de un trabajo y quedó atorado para siempre, porque
nadie retomaba los trabajos marcados como "en ejecución". Le puse un lease de diez minutos. Es
seguro porque el procesamiento recalcula desde la API y la ejecución tiene la idempotencia de
arriba.

**"¿Por qué no lo lanzaste?"**
Porque el riesgo que quedaba no era técnico, era de demanda, y validarlo pedía un trabajo de
ventas que no quise hacer en ese momento. Preferí ser explícito con eso antes que seguir
construyendo funcionalidad para un cliente que no existía todavía.

---

## Si piden ver código

- El modelo de reputación: `src/copiloto/decision/reputation.py` — es la pieza con más
  contenido propio y se explica sola en 5 minutos.
- La decisión: `src/copiloto/decision/recommender.py` — Monte Carlo, ranking, cuándo escalar a
  humano.
- La ejecución idempotente: `src/copiloto/actions.py` — la parte que más cuida el dinero.
- El contrato real de la API y las seis diferencias: `docs/PRUEBA_REAL.md`.

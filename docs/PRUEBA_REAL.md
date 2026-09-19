# Prueba real — runbook de validación contra Mercado Libre de verdad

Este documento es el experimento **fuera del repo**: todo lo que hay en `src/` se valida sin
red contra `copiloto.meli.fake` (`tests/`, `copiloto demo`). Esto es para cuando quieras
confirmar que el copiloto también funciona contra la Mercado Libre real.

> **Advertencia explícita**: no está documentado en ningún lugar oficial que los reclamos y las
> mediaciones de Mercado Libre funcionen con **usuarios de prueba** (`/users/test_user`). Es
> posible que llegues hasta "abrir el reclamo" y la API se comporte distinto a un caso real, o
> que ciertos endpoints devuelvan datos incompletos para cuentas de prueba. Este runbook asume
> que sí funciona y te dice qué observar; si no funciona, el **Plan B** (al final) es probar con
> un vendedor real en modo `shadow`, que nunca ejecuta nada.

## 0. Qué vas a necesitar

- Una cuenta de Mercado Libre México (puede ser la tuya, vas a operar en modo prueba).
- `cloudflared` o `ngrok` instalado (túnel HTTPS hacia tu máquina; ML exige HTTPS para el
  webhook y para el `redirect_uri` de OAuth).
- El repo con `make install` ya corrido (`.venv` listo).

## 1. Crear la app en DevCenter

1. Entra a [DevCenter](https://developers.mercadolibre.com.mx/devcenter) con tu cuenta.
2. Crea una aplicación nueva. Anota `App ID` y `Client Secret`.
3. **Redirect URI**: tiene que ser HTTPS. Si vas a probar en local, primero levanta el túnel
   (paso 2) y usa la URL que te da, con el path `/oauth/callback`, por ejemplo
   `https://algo-al-azar.trycloudflare.com/oauth/callback`.
4. En "Notificaciones" (Webhooks), activa el tópico **Post Purchase** con los filtros `claims`
   y `claims_actions`, apuntando a `https://<tu-túnel>/notifications`. Actívalo también para
   `orders_v2` y `shipments` si quieres ver esos eventos en los logs (el copiloto solo actúa
   sobre `claims`/`claims_actions`; los demás los responde 200 y los ignora), y para `messages`
   si quieres ver la conversación en tiempo real (tampoco dispara nada por sí solo).

## 2. Túnel HTTPS

```bash
cloudflared tunnel --url http://localhost:8000
# o: ngrok http 8000
```

Copia la URL pública HTTPS. Actualiza el `redirect_uri` de la app en DevCenter si cambió (los
túneles gratuitos de un solo uso generan una URL nueva cada vez que los reinicias).

## 3. Configurar y levantar el copiloto

```bash
cp .env.example .env
# completa: COPILOTO_APP_ID, COPILOTO_CLIENT_SECRET, COPILOTO_REDIRECT_URI (la del túnel),
# COPILOTO_SECRET_KEY (genera una fija, ver el comentario en .env.example), COPILOTO_MODE=shadow
set -a; source .env; set +a

.venv/bin/copiloto init-db
.venv/bin/copiloto serve --port 8000 &
.venv/bin/copiloto worker &
```

`COPILOTO_MODE=shadow` es innegociable para esta prueba: el copiloto va a clasificar,
recomendar y redactar, pero **nunca** va a llamar a `refund`/`allow-return`/`partial-refund` ni
mandar mensajes por ti.

## 4. Autorizar la app (OAuth)

Abre `https://<tu-túnel>/oauth/start` en el navegador, inicia sesión con tu cuenta de Mercado
Libre y autoriza. Deberías terminar en una página que dice "Cuenta conectada". Verifica:

```bash
.venv/bin/python -c "
from copiloto.config import Settings
from copiloto.store import Store
s = Settings.from_env()
store = Store(s.db_path, s.secret_key)
for seller in store.list_sellers():
    print(seller.id, seller.nickname, seller.needs_reauth)
"
```

## 5. Usuarios de prueba

```bash
export COPILOTO_TEST_ACCESS_TOKEN=<access token de un vendedor ya autorizado, el que acabas de conectar>
.venv/bin/python scripts/crear_usuarios_prueba.py
```

Crea 2 usuarios (vendedor y comprador) y los guarda en `data/test_users.json`. **No hay
endpoint para listarlos después**: ese archivo es tu única referencia, ábrelo si necesitas las
contraseñas otra vez.

## 6. Publicar un ítem de prueba

Con el usuario de prueba **vendedor** (o con tu cuenta real, si vas por el Plan B), publica un
artículo económico. El título es obligatorio y literal:

```
Item de Prueba - Por favor, NO OFERTAR
```

## 7. Comprar con tarjeta de prueba

Con el usuario de prueba **comprador**, compra el ítem usando una [tarjeta de prueba de
Mercado Pago México](https://www.mercadopago.com.mx/developers/es/docs/checkout-api/additional-content/test-cards).
Completa el pago hasta que la orden quede aprobada.

## 8. Abrir el reclamo desde la UI

Con el comprador de prueba, desde "Mis compras" abre un reclamo sobre la orden (elige cualquier
motivo, p. ej. "No lo recibí" si quieres forzar `PNR`, o "Es diferente/está dañado" para `PDD`).

## 9. Observar

- **Notificación**: revisa los logs del proceso `serve` — debería llegar el `POST
  /notifications` en segundos, responder 200, y (en los logs del `worker`) procesarse.
- **Worker en modo sombra**: confirma que se generó una recomendación y un borrador sin que se
  haya llamado a ningún endpoint de escritura:

  ```bash
  .venv/bin/copiloto stats
  # o directo al dashboard:
  open http://localhost:8000/
  ```

- Abre el reclamo en el dashboard y compara la categoría/acción/borrador contra lo que tú
  esperarías del caso real.

## 10. Checklist de qué endpoints respondieron

Marca cada uno conforme lo veas aparecer en los logs (nivel INFO/DEBUG) o confírmalo con
`copiloto stats` / mirando la tabla `events`:

- [ ] `GET /post-purchase/v1/claims/{id}`
- [ ] `GET /post-purchase/v1/claims/{id}/detail`
- [ ] `GET /post-purchase/v1/claims/reasons/{reason_id}`
- [ ] `GET /post-purchase/v1/claims/{id}/expected-resolutions`
- [ ] `GET /post-purchase/v1/claims/{id}/affects-reputation` (¿trae `has_incentive`?)
- [ ] `GET /post-purchase/v1/claims/{id}/partial-refund/available-offers` (solo si el reclamo
      lo permite)
- [ ] `GET /post-purchase/v1/claims/{id}/messages`
- [ ] `GET /orders/{id}`
- [ ] `GET /shipments/{id}` (con `x-format-new: true`)
- [ ] `GET /shipments/{id}/history`
- [ ] `GET /users/{seller_id}` (¿trae `seller_reputation.metrics`?)
- [ ] Notificación de `claims`/`claims_actions` recibida y deduplicada correctamente

Anota cualquier campo que falte o que venga con otro nombre del que asume `pipeline.py` — son
justo los puntos marcados `[supuesto]` en el código (ver `docs/ARQUITECTURA.md`).

## Plan B: sin usuarios de prueba

Si llegas al paso 8 y el reclamo no se comporta como uno real (o la documentación de tu cuenta
de pruebas no deja abrir reclamos), la alternativa es:

1. Usar tu cuenta **real** de vendedor (no una de prueba), autorizada igual que en el paso 4.
2. Dejar `COPILOTO_MODE=shadow` puesto (no negociable: nunca vas a querer que un experimento
   ejecute acciones reales de dinero sobre tu cuenta de verdad).
3. Esperar a que llegue un reclamo real de un comprador real, o pedirle a alguien de confianza
   que simule uno con una compra pequeña.
4. Validar el mismo checklist del paso 10, pero SIN aprobar/ejecutar nada — solo confirmar que
   clasificación, recomendación y borrador se ven razonables. Si algo se ve mal, es más barato
   corregirlo mirando el dashboard que descubrirlo con dinero real moviéndose.

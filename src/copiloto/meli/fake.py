"""meli/fake.py — simulador en FastAPI del subconjunto de la API de Mercado Libre que usamos.

No está documentado que reclamos/mediaciones funcionen con usuarios de prueba de ML (ver
ESPECIFICACION.md §3 y `docs/PRUEBA_REAL.md`), así que la validación de punta a punta del
copiloto —tests y `copiloto demo`— corre contra ESTE simulador, no contra la red. Reproduce
las formas de request/response verificadas en la especificación (endpoints, `affects-reputation`,
`available_offers`, el payload de notificación) y, donde la especificación no fija la forma
exacta del cuerpo (p. ej. `expected-resolutions`, `returns` v2), usa una forma razonable y
consistente con lo que `pipeline.py` espera — está documentado en cada sitio como [supuesto].

`FakeMeliState` es el estado en memoria (mutable, un `dict` por entidad); `create_fake_app`
lo envuelve en rutas FastAPI. Los dos se separan para que los tests puedan inspeccionar o
mutar el estado directamente (p. ej. "¿ya quedó cerrado el reclamo tras el refund?") sin tener
que parsear respuestas HTTP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile

# IDs de la lista de prueba: no son reales, solo identificadores consistentes dentro del fake.
DEFAULT_SELLER_ID = "900001"
DEFAULT_BUYER_ID = "900002"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _check_bearer(authorization: str) -> None:
    if not authorization.startswith("Bearer ") or len(authorization) <= len("Bearer "):
        raise HTTPException(status_code=401, detail="falta un Bearer token")


@dataclass
class FakeMeliState:
    """Todo el estado del ML simulado. Un `FakeMeliState` nuevo por test = aislamiento total."""

    app_id: str | None = None
    client_secret: str | None = None
    seller_id: str = DEFAULT_SELLER_ID
    seller_nickname: str = "TIENDA_DEMO"
    seller_level_id: str = "5_green"
    seller_power_status: str | None = None
    seller_sales_completed: int = 800
    seller_claims_value: int = 3
    seller_metrics_period: str = "60 days"

    claims: dict[str, dict[str, Any]] = field(default_factory=dict)
    reasons: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    shipments: dict[str, dict[str, Any]] = field(default_factory=dict)
    shipment_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    status_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    actions_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    expected_resolutions: dict[str, list[str]] = field(default_factory=dict)
    partial_offers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    returns_v2: dict[str, dict[str, Any]] = field(default_factory=dict)
    due_dates: dict[str, datetime] = field(default_factory=dict)
    attachments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    access_tokens: dict[str, str] = field(default_factory=dict)  # token -> seller_id
    auth_codes: dict[str, str] = field(default_factory=dict)  # code -> seller_id
    refresh_tokens: dict[str, str] = field(default_factory=dict)  # refresh_token -> seller_id
    missed_feed_claim_ids: list[str] = field(default_factory=list)
    claim_has_incentive: dict[str, bool] = field(default_factory=dict)
    _test_user_seq: int = 0

    # ── Autenticación ───────────────────────────────────────────────────────────────────

    def issue_tokens(self, seller_id: str) -> tuple[str, str]:
        access = f"access-{seller_id}-{uuid.uuid4().hex[:10]}"
        refresh = f"refresh-{seller_id}-{uuid.uuid4().hex[:10]}"
        self.access_tokens[access] = seller_id
        self.refresh_tokens[refresh] = seller_id
        return access, refresh

    def issue_auth_code(self, seller_id: str) -> str:
        """Simula "el comprador ya autorizó la app en el navegador de Mercado Libre": en la
        vida real esto lo genera ML y llega por el `redirect_uri`; aquí lo dispara quien
        orquesta el test/demo."""
        code = f"code-{seller_id}-{uuid.uuid4().hex[:10]}"
        self.auth_codes[code] = seller_id
        return code

    def seller_id_for_token(self, token: str) -> str | None:
        return self.access_tokens.get(token)

    def next_test_user_id(self) -> str:
        self._test_user_seq += 1
        return f"{990000 + self._test_user_seq}"

    # ── Reputación ──────────────────────────────────────────────────────────────────────

    def user_payload(self, user_id: str) -> dict:
        if user_id != self.seller_id:
            return {"id": user_id, "nickname": f"USER{user_id}"}
        return {
            "id": self.seller_id,
            "nickname": self.seller_nickname,
            "seller_reputation": {
                "level_id": self.seller_level_id,
                "power_seller_status": self.seller_power_status,
                "metrics": {
                    "claims": {"value": self.seller_claims_value, "period": self.seller_metrics_period},
                    "sales": {"completed": self.seller_sales_completed, "period": self.seller_metrics_period},
                },
            },
        }

    # ── Notificaciones ──────────────────────────────────────────────────────────────────

    def notification_payload(self, claim_id: str, topic: str = "claims") -> dict:
        return {
            "_id": uuid.uuid4().hex,
            "resource": f"/post-purchase/v1/claims/{claim_id}",
            "user_id": self.seller_id,
            "topic": topic,
            "application_id": self.app_id or "APPTEST",
            "attempts": 1,
            "sent": _iso(_now()),
            "received": _iso(_now()),
        }

    # ── Construcción de reclamos ────────────────────────────────────────────────────────

    def new_claim(
        self,
        claim_id: str,
        *,
        reason_id: str | None,
        reason_name: str,
        claim_type: str = "mediations",
        respondent_actions: list[str],
        order_amount: float,
        sale_fee: float,
        seller_sku: str,
        shipment_status: str,
        logistic_type: str,
        tracking_number: str,
        shipped_hours_ago: float | None,
        delivered_hours_ago: float | None,
        buyer_message: str | None,
        expected: list[str],
        partial_pcts: tuple[int, ...] = (),
        has_incentive: bool = True,
    ) -> dict:
        now = _now()
        order_id = f"O{claim_id}"
        shipment_id = f"S{claim_id}"
        self.orders[order_id] = {
            "id": order_id,
            "total_amount": order_amount,
            "order_items": [
                {
                    "item": {
                        "id": f"MLM{claim_id}",
                        "title": f"Producto de prueba {claim_id}",
                        "seller_sku": seller_sku,
                    },
                    "quantity": 1,
                    "unit_price": order_amount,
                    "sale_fee": sale_fee,
                }
            ],
            "shipping": {"id": shipment_id},
        }
        history = []
        if shipped_hours_ago is not None:
            history.append({"status": "shipped", "date": _iso(now - timedelta(hours=shipped_hours_ago))})
        if delivered_hours_ago is not None:
            history.append({"status": "delivered", "date": _iso(now - timedelta(hours=delivered_hours_ago))})
        self.shipment_history[shipment_id] = history
        self.shipments[shipment_id] = {
            "id": shipment_id,
            "status": shipment_status,
            "substatus": None,
            "logistic_type": logistic_type,
            "tracking_number": tracking_number,
        }
        if reason_id:
            self.reasons[reason_id] = {"id": reason_id, "name": reason_name, "detail": None}
        due = now + timedelta(hours=48)
        self.due_dates[claim_id] = due
        claim = {
            "id": claim_id,
            "status": "opened",
            "stage": "claim",
            "type": claim_type,
            "reason_id": reason_id,
            "resource": f"claims/{claim_id}",
            "resource_id": claim_id,
            "related_entities": [{"type": "order", "id": order_id}],
            "date_created": _iso(now),
            "players": [
                {"role": "complainant", "type": "buyer", "user_id": DEFAULT_BUYER_ID, "available_actions": []},
                {
                    "role": "respondent",
                    "type": "seller",
                    "user_id": self.seller_id,
                    "available_actions": [
                        {"action": a, "due_date": _iso(due), "mandatory": a in ("send_message_to_complainant",)}
                        for a in respondent_actions
                    ],
                },
            ],
            "resolution": None,
        }
        self.claims[claim_id] = claim
        self.messages[claim_id] = []
        if buyer_message:
            self.messages[claim_id].append({"sender_role": "complainant", "message": buyer_message, "date": _iso(now)})
        self.status_history[claim_id] = [{"stage": "claim", "date": _iso(now)}]
        self.expected_resolutions[claim_id] = expected
        self.partial_offers[claim_id] = [
            {"amount": round(order_amount * p / 100, 2), "percentage": p} for p in partial_pcts
        ]
        self.returns_v2[claim_id] = {"warehouse_review": {"result": None}}
        self.claim_has_incentive[claim_id] = has_incentive
        return claim

    def claim_view(self, claim_id: str) -> dict:
        """Copia "viva" del reclamo: si ya está cerrado, no quedan acciones disponibles."""
        claim = self.claims.get(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail=f"reclamo {claim_id} no existe")
        view = dict(claim)
        view["players"] = [dict(p) for p in claim["players"]]
        if claim["status"] != "opened":
            for p in view["players"]:
                if p["role"] == "respondent":
                    p["available_actions"] = []
        return view

    def log_action(self, claim_id: str, action_name: str, role: str = "respondent") -> None:
        claim = self.claims.get(claim_id, {})
        self.actions_history.setdefault(claim_id, []).append(
            {
                "action_name": action_name,
                "player_role": role,
                "claim_stage": claim.get("stage"),
                "claim_status": claim.get("status"),
                "date": _iso(_now()),
            }
        )

    def affects_reputation_payload(self, claim_id: str) -> dict:
        has_incentive = self.claim_has_incentive.get(claim_id, True)
        claim = self.claims.get(claim_id, {})
        if claim.get("status") != "opened":
            has_incentive = False
        due = self.due_dates.get(claim_id)
        return {
            # Vocabulario verificado: affected | not_affected | not_applies.
            "affects_reputation": "not_affected" if has_incentive else "affected",
            "has_incentive": has_incentive,
            "due_date": _iso(due) if (due and has_incentive) else None,
        }


def seed_default_scenarios(state: FakeMeliState) -> FakeMeliState:
    """Los 7 escenarios de la especificación (§9): cubren las categorías y los regímenes de
    reputación que ejercitan el pipeline completo en `copiloto demo` y en los tests E2E."""
    # 1) PNR entregado con evidencia fuerte → debería defender.
    state.new_claim(
        "1001",
        reason_id="PNR3430",
        reason_name="El paquete no llegó",
        respondent_actions=["refund", "send_message_to_complainant"],
        order_amount=899.0,
        sale_fee=89.9,
        seller_sku="SKU-PNR-FUERTE",
        shipment_status="delivered",
        logistic_type="drop_off",
        tracking_number="MLXX00000001",
        shipped_hours_ago=72,
        delivered_hours_ago=30,
        buyer_message="No he recibido mi pedido, ya pasaron varios días y sigo esperando.",
        expected=["refund"],
    )
    # 2) PNR en tránsito → debería informar rastreo.
    state.new_claim(
        "1002",
        reason_id="PNR3430",
        reason_name="El paquete no llegó",
        respondent_actions=["refund", "send_message_to_complainant"],
        order_amount=650.0,
        sale_fee=65.0,
        seller_sku="SKU-PNR-TRANSITO",
        shipment_status="shipped",
        logistic_type="fulfillment",
        tracking_number="MLXX00000002",
        shipped_hours_ago=20,
        delivered_hours_ago=None,
        buyer_message="¿Cuándo llega mi pedido? Todavía no lo recibo.",
        expected=["refund"],
    )
    # 3) PDD defectuoso.
    state.new_claim(
        "2001",
        reason_id="PDD2",
        reason_name="El paquete llegó dañado",
        respondent_actions=["refund", "allow_return", "allow_partial_refund", "send_message_to_complainant"],
        order_amount=1200.0,
        sale_fee=120.0,
        seller_sku="SKU-DEFECTUOSO",
        shipment_status="delivered",
        logistic_type="fulfillment",
        tracking_number="MLXX00000003",
        shipped_hours_ago=96,
        delivered_hours_ago=40,
        buyer_message="El producto llegó roto, no funciona.",
        expected=["refund"],
        partial_pcts=(10, 20, 30),
    )
    # 4) PDD diferente.
    state.new_claim(
        "2002",
        reason_id="PDD100",
        reason_name="El producto es diferente al publicado",
        respondent_actions=["refund", "allow_return", "allow_partial_refund", "send_message_to_complainant"],
        order_amount=780.0,
        sale_fee=78.0,
        seller_sku="SKU-DIFERENTE",
        shipment_status="delivered",
        logistic_type="cross_docking",
        tracking_number="MLXX00000004",
        shipped_hours_ago=90,
        delivered_hours_ago=36,
        buyer_message="Me llegó otro modelo, no es el que pedí.",
        expected=["change_product"],
        partial_pcts=(10, 20, 30, 40),
    )
    # 5) PDD arrepentimiento (devolución).
    state.new_claim(
        "2003",
        reason_id="PDD9939",
        reason_name="Llegó en buenas condiciones pero no lo quiero",
        respondent_actions=["allow_return", "allow_partial_refund", "send_message_to_complainant"],
        order_amount=540.0,
        sale_fee=54.0,
        seller_sku="SKU-ARREPENTIMIENTO",
        shipment_status="delivered",
        logistic_type="drop_off",
        tracking_number="MLXX00000005",
        shipped_hours_ago=60,
        delivered_hours_ago=24,
        buyer_message="Llegó bien pero ya no lo quiero, cambié de opinión.",
        expected=["refund"],
        partial_pcts=(10, 20),
    )
    # 6) Incompleto.
    state.new_claim(
        "3001",
        reason_id="PDD101",
        reason_name="Falta contenido en el paquete",
        respondent_actions=["refund", "allow_return", "allow_partial_refund", "send_message_to_complainant"],
        order_amount=430.0,
        sale_fee=43.0,
        seller_sku="SKU-INCOMPLETO",
        shipment_status="delivered",
        logistic_type="fulfillment",
        tracking_number="MLXX00000006",
        shipped_hours_ago=80,
        delivered_hours_ago=32,
        buyer_message="Le faltan piezas, no venía completo.",
        expected=["product"],
        partial_pcts=(10, 20, 30),
    )
    # 7) Cancelación con el paquete ya despachado.
    state.new_claim(
        "4001",
        reason_id="CS1",
        reason_name="El comprador quiere cancelar la compra",
        claim_type="cancel_purchase",
        respondent_actions=["refund", "send_message_to_complainant"],
        order_amount=300.0,
        sale_fee=30.0,
        seller_sku="SKU-CANCELACION",
        shipment_status="shipped",
        logistic_type="drop_off",
        tracking_number="MLXX00000007",
        shipped_hours_ago=5,
        delivered_hours_ago=None,
        buyer_message="Ya no quiero la compra, cancelen por favor.",
        expected=["refund"],
    )
    return state


def create_fake_app(state: FakeMeliState | None = None) -> tuple[FastAPI, FakeMeliState]:
    """Devuelve `(app, state)`: el `state` queda accesible para que tests/demo lo inspeccionen
    o lo muten directamente sin pasar por HTTP."""
    state = state if state is not None else FakeMeliState()
    app = FastAPI(title="Mercado Libre (fake)")

    def _claim_or_404(claim_id: str) -> dict:
        if claim_id not in state.claims:
            raise HTTPException(status_code=404, detail=f"reclamo {claim_id} no existe")
        return state.claims[claim_id]

    # ── OAuth ───────────────────────────────────────────────────────────────────────────

    @app.post("/oauth/token")
    async def oauth_token(request: Request) -> dict:
        form = await request.form()
        client_id = form.get("client_id")
        if state.app_id and client_id != state.app_id:
            raise HTTPException(status_code=400, detail="invalid_client: client_id no coincide")
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            code = str(form.get("code") or "")
            seller_id = state.auth_codes.pop(code, None)
            if seller_id is None:
                raise HTTPException(status_code=400, detail="invalid_grant: code inválido, expirado o ya usado")
        elif grant_type == "refresh_token":
            rt = str(form.get("refresh_token") or "")
            seller_id = state.refresh_tokens.pop(rt, None)
            if seller_id is None:
                raise HTTPException(status_code=400, detail="invalid_grant: refresh_token inválido o ya usado")
        else:
            raise HTTPException(status_code=400, detail="unsupported_grant_type")
        access_token, refresh_token_new = state.issue_tokens(seller_id)
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 10_800,
            "scope": "offline_access read write",
            "user_id": seller_id,
            "refresh_token": refresh_token_new,
        }

    @app.get("/users/me")
    def users_me(authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        token = authorization.removeprefix("Bearer ")
        seller_id = state.seller_id_for_token(token)
        if seller_id is None:
            raise HTTPException(status_code=401, detail="token desconocido")
        return state.user_payload(seller_id)

    @app.post("/users/test_user")
    def create_test_user(authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        new_id = state.next_test_user_id()
        return {
            "id": new_id,
            "nickname": f"TEST{new_id}",
            "password": "qatest1234",
            "site_status": "active",
            "email": f"test_user_{new_id}@testuser.com",
        }

    # ── Reclamos ────────────────────────────────────────────────────────────────────────

    @app.get("/post-purchase/v1/claims/search")
    def search_claims(
        status: str | None = Query(default=None),
        limit: int = Query(default=50),
        offset: int = Query(default=0),
        players_role: str | None = Query(default=None, alias="players.role"),
        players_user_id: str | None = Query(default=None, alias="players.user_id"),
        authorization: str = Header(default=""),
    ) -> dict:
        _check_bearer(authorization)
        del players_role
        results = []
        for claim in state.claims.values():
            if status is not None and claim["status"] != status:
                continue
            if players_user_id is not None:
                uids = {p["user_id"] for p in claim["players"]}
                if players_user_id not in uids:
                    continue
            results.append(state.claim_view(claim["id"]))
        page = results[offset : offset + limit]
        return {"results": page, "paging": {"total": len(results), "limit": limit, "offset": offset}}

    @app.get("/post-purchase/v1/claims/reasons/{reason_id}")
    def get_reason(reason_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        return state.reasons.get(reason_id, {"id": reason_id, "name": None, "detail": None})

    @app.get("/post-purchase/v1/claims/{claim_id}")
    def get_claim(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        return state.claim_view(claim_id)

    @app.get("/post-purchase/v1/claims/{claim_id}/detail")
    def get_claim_detail(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        due = state.due_dates.get(claim_id)
        return {"due_date": _iso(due) if due else None, "action_responsible": "respondent"}

    @app.get("/post-purchase/v1/claims/{claim_id}/expected-resolutions")
    def get_expected_resolutions(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        actions = state.expected_resolutions.get(claim_id, [])
        return {"expected_resolutions": [{"action": a} for a in actions]}

    @app.get("/post-purchase/v1/claims/{claim_id}/partial-refund/available-offers")
    def get_partial_offers(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        return {"available_offers": state.partial_offers.get(claim_id, [])}

    @app.get("/post-purchase/v1/claims/{claim_id}/affects-reputation")
    def get_affects_reputation(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        return state.affects_reputation_payload(claim_id)

    @app.get("/post-purchase/v1/claims/{claim_id}/messages")
    def get_messages(claim_id: str, authorization: str = Header(default="")) -> list[dict]:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        return state.messages.get(claim_id, [])

    @app.post("/post-purchase/v1/claims/{claim_id}/actions/send-message")
    async def send_message(claim_id: str, request: Request, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        body = await request.json()
        entry = {
            "sender_role": "respondent",
            "receiver_role": body.get("receiver_role", "complainant"),
            "message": body.get("message", ""),
            "attachments": body.get("attachments", []),
            "date": _iso(_now()),
        }
        state.messages.setdefault(claim_id, []).append(entry)
        state.log_action(claim_id, f"send_message_to_{entry['receiver_role']}")
        return {"id": f"msg-{len(state.messages[claim_id])}", **entry}

    @app.post("/post-purchase/v1/claims/{claim_id}/attachments")
    async def upload_attachment(
        claim_id: str,
        file: UploadFile = File(...),  # noqa: B008 (patrón estándar de FastAPI)
        authorization: str = Header(default=""),
    ) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        content = await file.read()
        att_id = f"att-{uuid.uuid4().hex[:10]}"
        record = {"id": att_id, "filename": file.filename, "size": len(content)}
        state.attachments.setdefault(claim_id, []).append(record)
        return record

    @app.post("/post-purchase/v1/claims/{claim_id}/expected-resolutions/refund")
    def refund(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        claim = _claim_or_404(claim_id)
        state.log_action(claim_id, "refund")
        claim["status"] = "closed"
        claim["resolution"] = {"reason": "payment_refunded", "benefited": ["complainant"], "closed_by": "respondent"}
        return {"status": claim["status"], "resolution": claim["resolution"]}

    @app.post("/post-purchase/v1/claims/{claim_id}/expected-resolutions/allow-return")
    def allow_return(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        claim = _claim_or_404(claim_id)
        state.log_action(claim_id, "allow_return")
        claim["resolution"] = {"reason": "return_authorized", "benefited": [], "closed_by": None}
        return {"status": claim["status"], "resolution": claim["resolution"]}

    @app.post("/post-purchase/v1/claims/{claim_id}/expected-resolutions/partial-refund")
    async def partial_refund(claim_id: str, request: Request, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        claim = _claim_or_404(claim_id)
        body = await request.json()
        pct = body.get("percentage")
        state.log_action(claim_id, "allow_partial_refund")
        claim["pending_partial_offer"] = pct
        return {"status": claim["status"], "pending_partial_offer": pct}

    @app.post("/post-purchase/v1/claims/{claim_id}/actions/open-dispute")
    def open_dispute(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        claim = _claim_or_404(claim_id)
        claim["stage"] = "dispute"
        state.status_history.setdefault(claim_id, []).append({"stage": "dispute", "date": _iso(_now())})
        return {"status": claim["status"], "stage": claim["stage"]}

    @app.get("/post-purchase/v1/claims/{claim_id}/status-history")
    def get_status_history(claim_id: str, authorization: str = Header(default="")) -> list[dict]:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        return state.status_history.get(claim_id, [])

    @app.get("/post-purchase/v1/claims/{claim_id}/actions-history")
    def get_actions_history(claim_id: str, authorization: str = Header(default="")) -> list[dict]:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        return state.actions_history.get(claim_id, [])

    @app.get("/post-purchase/v2/claims/{claim_id}/returns")
    def get_returns(claim_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        _claim_or_404(claim_id)
        return state.returns_v2.get(claim_id, {"warehouse_review": {"result": None}})

    # ── Órdenes y envíos ────────────────────────────────────────────────────────────────

    @app.get("/orders/{order_id}")
    def get_order(order_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        if order_id not in state.orders:
            raise HTTPException(status_code=404, detail=f"orden {order_id} no existe")
        return state.orders[order_id]

    @app.get("/shipments/{shipment_id}")
    def get_shipment(
        shipment_id: str, authorization: str = Header(default=""), x_format_new: str | None = Header(default=None)
    ) -> dict:
        _check_bearer(authorization)
        del x_format_new
        if shipment_id not in state.shipments:
            raise HTTPException(status_code=404, detail=f"envío {shipment_id} no existe")
        return state.shipments[shipment_id]

    @app.get("/shipments/{shipment_id}/history")
    def get_shipment_history(shipment_id: str, authorization: str = Header(default="")) -> list[dict]:
        _check_bearer(authorization)
        return state.shipment_history.get(shipment_id, [])

    # ── Usuario / reputación ────────────────────────────────────────────────────────────

    @app.get("/users/{user_id}")
    def get_user(user_id: str, authorization: str = Header(default="")) -> dict:
        _check_bearer(authorization)
        return state.user_payload(user_id)

    # ── Notificaciones perdidas ─────────────────────────────────────────────────────────

    @app.get("/missed_feeds")
    def missed_feeds(
        app_id: str = Query(...), topic: str = Query(default="claims"), authorization: str = Header(default="")
    ) -> list[dict]:
        _check_bearer(authorization)
        del app_id
        return [state.notification_payload(cid, topic=topic) for cid in state.missed_feed_claim_ids]

    return app, state

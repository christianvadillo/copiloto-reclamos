"""cli.py — entrypoint `copiloto` (argparse): operar el servicio desde la terminal.

`demo` es el comando que prueba que todo el sistema funciona de punta a punta SIN red ni
credenciales: levanta el simulador de Mercado Libre (`meli.fake`) en proceso, una base SQLite
temporal, y —si no hay `ANTHROPIC_API_KEY`— redacta con plantillas. Es intencional que sea
determinista y offline: es la forma de validar un cambio en segundos, no en minutos contra la
red real (para eso está `docs/PRUEBA_REAL.md`).
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import UTC, datetime, timedelta

from copiloto.config import Settings
from copiloto.meli.client import MeliClient
from copiloto.meli.oauth import TokenProvider, authorization_url, generate_state
from copiloto.store import Store
from copiloto.worker import Worker


def _build_store(settings: Settings) -> Store:
    return Store(settings.db_path, settings.secret_key)


def _build_llm_client(settings: Settings):
    if not settings.llm_enabled:
        return None
    import anthropic

    return anthropic.Anthropic()


def cmd_init_db(_args: argparse.Namespace, settings: Settings) -> int:
    store = _build_store(settings)
    store.close()
    print(f"base de datos lista en {settings.db_path}")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from copiloto.app import create_app

    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_worker(args: argparse.Namespace, settings: Settings) -> int:
    import logging

    logging.basicConfig(level=logging.INFO)
    store = _build_store(settings)
    token_provider = TokenProvider(store, settings)
    meli = MeliClient(settings.meli_base_url, token_provider)
    worker = Worker(store, settings, meli, llm_client=_build_llm_client(settings), poll_interval=args.poll_interval)
    worker.run_forever()
    return 0


def cmd_reconcile_once(_args: argparse.Namespace, settings: Settings) -> int:
    from copiloto import pipeline

    store = _build_store(settings)
    token_provider = TokenProvider(store, settings)
    meli = MeliClient(settings.meli_base_url, token_provider)
    total = sum(
        pipeline.reconcile_seller(store=store, settings=settings, meli=meli, seller_id=seller.id)
        for seller in store.list_sellers()
    )
    worker = Worker(store, settings, meli, llm_client=_build_llm_client(settings))
    worker.run_until_idle()
    print(f"reconciliación: {total} job(s) encolados, cola vaciada")
    return 0


def cmd_auth_url(_args: argparse.Namespace, settings: Settings) -> int:
    print(authorization_url(settings, generate_state()))
    return 0


def cmd_stats(_args: argparse.Namespace, settings: Settings) -> int:
    store = _build_store(settings)
    print("Jobs:", store.job_counts())
    print("Eventos:", store.event_kind_counts())
    latencies = sorted(store.notification_to_draft_latencies_seconds())
    if latencies:

        def pct(q: float) -> float:
            idx = min(len(latencies) - 1, max(0, round(q * (len(latencies) - 1))))
            return latencies[idx]

        print(
            f"Notificación → borrador: p50={statistics.median(latencies):.1f}s p90={pct(0.9):.1f}s (n={len(latencies)})"
        )
    else:
        print("Notificación → borrador: sin datos todavía")
    return 0


def _print_table(headers: list[str], rows: list[tuple]) -> None:
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in str_rows:
        print(fmt(row))


def cmd_demo(_args: argparse.Namespace, settings: Settings) -> int:
    import tempfile

    from fastapi.testclient import TestClient

    from copiloto.app import create_app
    from copiloto.meli.fake import DEFAULT_SELLER_ID, FakeMeliState, create_fake_app, seed_default_scenarios

    tmp_dir = tempfile.mkdtemp(prefix="copiloto-demo-")
    demo_settings = settings.model_copy(
        update={
            "db_path": f"{tmp_dir}/copiloto-demo.db",
            "mode": "approve",
            "llm_enabled": False,  # el demo NUNCA toca la red, ni siquiera si hay ANTHROPIC_API_KEY
            "app_id": "APPDEMO",
        }
    )

    fake_state = FakeMeliState(app_id=demo_settings.app_id)
    seed_default_scenarios(fake_state)
    fake_app, fake_state = create_fake_app(fake_state)
    fake_client = TestClient(fake_app)

    store = _build_store(demo_settings)
    access_token, refresh_token = fake_state.issue_tokens(DEFAULT_SELLER_ID)
    store.upsert_seller(
        DEFAULT_SELLER_ID,
        fake_state.seller_nickname,
        access_token,
        refresh_token,
        datetime.now(UTC) + timedelta(hours=3),
    )

    token_provider = TokenProvider(store, demo_settings, http_client=fake_client)
    meli = MeliClient(demo_settings.meli_base_url, token_provider, http_client=fake_client, sleep_fn=lambda _s: None)
    worker = Worker(store, demo_settings, meli, llm_client=None)

    app = create_app(demo_settings, store=store, http_client=fake_client)
    app_client = TestClient(app)

    claim_ids = sorted(fake_state.claims.keys())
    print(f"→ Enviando {len(claim_ids)} notificaciones al webhook (offline, API falsa)...")
    for claim_id in claim_ids:
        resp = app_client.post("/notifications", json=fake_state.notification_payload(claim_id))
        if resp.status_code != 200:
            print(f"  aviso: notificación de {claim_id} respondió {resp.status_code}: {resp.text}", file=sys.stderr)

    print("→ Procesando la cola con el worker...")
    n = worker.run_until_idle()
    print(f"  {n} job(s) procesados.\n")

    rows = []
    for claim_id in claim_ids:
        claim_row = store.get_claim_row(claim_id)
        rec = store.get_latest_recommendation(claim_id)
        draft = store.get_latest_draft(claim_id)
        preview = ""
        if draft:
            msg = draft["message"]
            preview = msg if len(msg) <= 42 else msg[:42] + "…"
        rows.append(
            (
                claim_id,
                claim_row["category"] if claim_row else "?",
                rec["action"] if rec else "?",
                f"${rec['expected_cost']:,.0f}" if rec else "?",
                f"{rec['prob_best']:.0%}" if rec else "?",
                f"${rec['reputation_lambda']:,.0f}" if rec else "?",
                preview,
            )
        )
    _print_table(["Reclamo", "Categoría", "Acción", "E[costo]", "P(mejor)", "λ", "Borrador (inicio)"], rows)

    # Preferimos un reclamo cuya acción mueva dinero (más vistoso); si ninguna la mueve, el
    # primero sirve igual: `execution_plan` siempre manda el mensaje aunque la acción sea
    # "defender" o "informar rastreo", así que el conteo de mensajes prueba la ejecución de
    # punta a punta pase lo que pase.
    money_actions = {"refund_full", "return_refund", "partial_refund"}
    target = next(
        (cid for cid in claim_ids if (store.get_latest_recommendation(cid) or {}).get("action") in money_actions),
        claim_ids[0],
    )
    draft = store.get_latest_draft(target)
    rec = store.get_latest_recommendation(target)
    print(f"\n→ Aprobando y ejecutando el reclamo {target} (acción: {rec['action'] if rec else '?'})...")
    before_status = fake_state.claims[target]["status"]
    before_msgs = len(fake_state.messages.get(target, []))
    resp = app_client.post(f"/claims/{target}/approve", data={"message": draft["message"] if draft else ""})
    if resp.status_code not in (200, 303):
        print(f"  aviso: aprobar respondió {resp.status_code}: {resp.text}", file=sys.stderr)
    worker.run_until_idle()
    after_status = fake_state.claims[target]["status"]
    after_msgs = len(fake_state.messages.get(target, []))
    pending_offer = fake_state.claims[target].get("pending_partial_offer")
    print(f"  mensajes al comprador en Mercado Libre (simulado): {before_msgs} → {after_msgs}")
    print(f"  estado del reclamo: {before_status} → {after_status}")
    if pending_offer is not None:
        print(f"  oferta de reembolso parcial registrada en Mercado Libre (simulado): {pending_offer}%")

    print(f"\nBase de datos temporal del demo: {demo_settings.db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="copiloto", description="Copiloto de reclamos para Mercado Libre México.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="levanta el webhook + dashboard (uvicorn)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="corre el worker que procesa la cola de jobs")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("reconcile-once", help="reconcilia todos los vendedores una vez y sale")
    p.set_defaults(func=cmd_reconcile_once)

    p = sub.add_parser("init-db", help="crea/migra la base de datos y sale")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("auth-url", help="imprime la URL de autorización OAuth")
    p.set_defaults(func=cmd_auth_url)

    p = sub.add_parser("stats", help="conteos de jobs/eventos y latencia notificación→borrador")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("demo", help="corre el flujo completo offline contra la API falsa")
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    return args.func(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""scripts/crear_usuarios_prueba.py — crea usuarios de prueba de Mercado Libre (MLM).

Parte del runbook de validación real (`docs/PRUEBA_REAL.md`): `POST /users/test_user` con el
access token de una app ya autorizada crea una cuenta de prueba (vendedor o comprador) en el
sandbox de Mercado Libre. Mercado Libre **no expone un endpoint para listar** los usuarios de
prueba ya creados —solo para crearlos—, así que este script los guarda en
`data/test_users.json` (gitignored) la primera vez y, si el archivo ya tiene los que se
pidieron, no crea más (hay un máximo de 10 por cuenta y se borran solos a los 60 días sin uso).

Uso (desde la raíz del repo, con una cuenta ya autorizada en /oauth/start):
    .venv/bin/python scripts/crear_usuarios_prueba.py          # usa la conexión guardada
    .venv/bin/python scripts/crear_usuarios_prueba.py --token APP_USR-...   # o un token explícito

El token nunca se imprime. Las contraseñas de los usuarios creados solo quedan en el archivo
de salida (permisos 0600); en pantalla se muestran id y nickname.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

MELI_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_OUTPUT = Path("data/test_users.json")
ROLES = ("vendedor", "comprador")


def create_test_user(client: httpx.Client, access_token: str) -> dict:
    resp = client.post(
        f"{MELI_BASE_URL}/users/test_user",
        json={"site_id": "MLM"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()


def _stored_access_token() -> str | None:
    """Access token de la primera cuenta autorizada en el copiloto (se refresca si hace falta)."""
    try:
        from copiloto.cli import _load_dotenv

        _load_dotenv()
        from copiloto.config import Settings
        from copiloto.meli.oauth import TokenProvider
        from copiloto.store import Store

        settings = Settings.from_env()
        store = Store(settings.db_path, settings.secret_key)
        sellers = store.list_sellers()
        if not sellers:
            return None
        return TokenProvider(store, settings).get(sellers[0].id)
    except Exception as exc:  # sin copiloto configurado: se pide --token
        print(f"No se pudo usar la conexión guardada: {exc}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--n", type=int, default=2, help="cuántos usuarios de prueba crear (por defecto 2: vendedor y comprador)"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("COPILOTO_TEST_ACCESS_TOKEN"),
        help="access token de una app ya autorizada; por defecto COPILOTO_TEST_ACCESS_TOKEN",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"dónde guardar los usuarios creados (por defecto {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)

    if not args.token:
        args.token = _stored_access_token()
    if not args.token:
        print(
            "Falta el access token: autoriza una cuenta en /oauth/start, o pasa --token / COPILOTO_TEST_ACCESS_TOKEN.",
            file=sys.stderr,
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if args.output.exists():
        existing = json.loads(args.output.read_text(encoding="utf-8"))

    if len(existing) >= args.n:
        print(f"Ya hay {len(existing)} usuario(s) de prueba en {args.output}; no se crean más.")
        print("Recuerda: Mercado Libre no expone un endpoint para LISTAR usuarios de prueba, solo para crearlos.")
        return 0

    created = list(existing)
    with httpx.Client(timeout=15.0) as client:
        while len(created) < args.n:
            role = ROLES[len(created)] if len(created) < len(ROLES) else f"extra_{len(created)}"
            try:
                user = create_test_user(client, args.token)
            except httpx.HTTPStatusError as exc:
                print(f"Mercado Libre respondió {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
                return 1
            user["rol_sugerido"] = role
            created.append(user)
            print(f"Creado ({role}): id={user.get('id')} nickname={user.get('nickname')}")

    args.output.write_text(json.dumps(created, indent=2, ensure_ascii=False), encoding="utf-8")
    args.output.chmod(0o600)  # trae contraseñas
    print(f"\nGuardado en {args.output} ({len(created)} usuario(s)).")
    print("Aviso: no hay endpoint de Mercado Libre para listar usuarios de prueba después de creados;")
    print("este archivo es la única forma de recuperarlos. No se sube a git (ver .gitignore).")
    print("Las contraseñas están en ese archivo; ábrelo tú cuando vayas a iniciar sesión con ellos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

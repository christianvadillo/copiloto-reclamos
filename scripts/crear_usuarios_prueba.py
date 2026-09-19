#!/usr/bin/env python3
"""scripts/crear_usuarios_prueba.py — crea usuarios de prueba de Mercado Libre (MLM).

Parte del runbook de validación real (`docs/PRUEBA_REAL.md`): `POST /users/test_user` con el
access token de una app ya autorizada crea una cuenta de prueba (vendedor o comprador) en el
sandbox de Mercado Libre. Mercado Libre **no expone un endpoint para listar** los usuarios de
prueba ya creados —solo para crearlos—, así que este script los guarda en
`data/test_users.json` (gitignored) la primera vez y, si el archivo ya tiene los que se
pidieron, no crea más (hay un máximo de 10 por cuenta y se borran solos a los 60 días sin uso).

Uso:
    COPILOTO_TEST_ACCESS_TOKEN=APP_USR-... python scripts/crear_usuarios_prueba.py
    python scripts/crear_usuarios_prueba.py --n 2 --token APP_USR-...
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
        print("Falta el access token: pasa --token o define COPILOTO_TEST_ACCESS_TOKEN.", file=sys.stderr)
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
    print(f"\nGuardado en {args.output} ({len(created)} usuario(s)).")
    print("Aviso: no hay endpoint de Mercado Libre para listar usuarios de prueba después de creados;")
    print("este archivo es la única forma de recuperarlos. No se sube a git (ver .gitignore).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

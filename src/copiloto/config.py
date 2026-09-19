"""config.py — configuración del copiloto, cargada de variables de entorno.

No usamos `pydantic-settings` (no está en las dependencias aprobadas): `Settings` es un
`BaseModel` normal y `Settings.from_env()` lee `os.environ` a mano con el prefijo
`COPILOTO_`. Mantenerlo así de simple evita una dependencia más y deja el parseo de tipos
(bool/int/float) explícito y fácil de auditar.

Todo lo que toca dinero real (montos de la política, costos de logística) vive aquí para que
cambiarlo no requiera tocar código; ver `.env.example` para la lista documentada.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TypeVar

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field

T = TypeVar("T")

ENV_PREFIX = "COPILOTO_"


def _env(name: str) -> str | None:
    return os.environ.get(f"{ENV_PREFIX}{name}")


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _get(name: str, default: T, caster: Callable[[str], T]) -> T:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        return caster(raw)
    except (ValueError, TypeError):
        return default


class Settings(BaseModel):
    """Configuración completa del servicio. Construirla con `Settings.from_env()`."""

    # ── Credenciales y OAuth de la app de Mercado Libre ────────────────────────────────────
    app_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost:8000/oauth/callback"

    # ── Cifrado en reposo de tokens (Fernet, 32 bytes url-safe base64) ─────────────────────
    secret_key: str = Field(default_factory=lambda: Fernet.generate_key().decode())

    # ── Almacenamiento ──────────────────────────────────────────────────────────────────────
    db_path: str = "data/copiloto.db"

    # ── Endpoints de Mercado Libre (§3 de la especificación) ───────────────────────────────
    meli_base_url: str = "https://api.mercadolibre.com"
    auth_base_url: str = "https://auth.mercadolibre.com.mx"

    # ── Política de ejecución (ver decision/recommender.py:Policy) ────────────────────────
    mode: str = "shadow"  # shadow | approve | auto
    auto_max_amount: float = 1500.0
    min_prob_best: float = 0.6

    # ── Redacción ───────────────────────────────────────────────────────────────────────────
    message_max_chars: int = 350
    llm_model: str = "claude-opus-5"
    llm_effort: str = "medium"
    llm_enabled: bool = Field(default_factory=lambda: bool(os.environ.get("ANTHROPIC_API_KEY")))

    # ── Webhook / seguridad ─────────────────────────────────────────────────────────────────
    ip_allowlist_enabled: bool = False
    trusted_proxy: bool = False

    # ── Evidencia local (fotos subidas por el vendedor, opcional) ─────────────────────────
    evidence_photos_dir: str | None = None

    # ── Economía (decision/domain.py:Economics; ver README para cómo se calibran) ─────────
    level_drop_cost: float = 30_000.0
    cogs_ratio: float = 0.6
    sku_costs_path: str | None = None
    return_shipping_cost: float = 120.0
    resend_shipping_cost: float = 120.0
    handling_cost: float = 40.0
    mediation_labor_cost: float = 150.0
    exchange_available: bool = False

    # ── Redacción: identidad de la tienda ──────────────────────────────────────────────────
    store_name: str = "Mi Tienda"
    signature: str = "El equipo de Mi Tienda"

    # ── Operación ───────────────────────────────────────────────────────────────────────────
    reconcile_interval_min: int = 15
    dashboard_user: str | None = None
    dashboard_password: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        kwargs: dict[str, object] = {}
        if (v := _env("APP_ID")) is not None:
            kwargs["app_id"] = v
        if (v := _env("CLIENT_SECRET")) is not None:
            kwargs["client_secret"] = v
        if (v := _env("REDIRECT_URI")) is not None:
            kwargs["redirect_uri"] = v
        if (v := _env("SECRET_KEY")) is not None:
            kwargs["secret_key"] = v
        if (v := _env("DB_PATH")) is not None:
            kwargs["db_path"] = v
        if (v := _env("MELI_BASE_URL")) is not None:
            kwargs["meli_base_url"] = v
        if (v := _env("AUTH_BASE_URL")) is not None:
            kwargs["auth_base_url"] = v
        if (v := _env("MODE")) is not None:
            kwargs["mode"] = v
        kwargs["auto_max_amount"] = _get("AUTO_MAX_AMOUNT", 1500.0, float)
        kwargs["min_prob_best"] = _get("MIN_PROB_BEST", 0.6, float)
        kwargs["message_max_chars"] = _get("MESSAGE_MAX_CHARS", 350, int)
        if (v := _env("LLM_MODEL")) is not None:
            kwargs["llm_model"] = v
        if (v := _env("LLM_EFFORT")) is not None:
            kwargs["llm_effort"] = v
        default_llm_enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
        kwargs["llm_enabled"] = _get("LLM_ENABLED", default_llm_enabled, _parse_bool)
        kwargs["ip_allowlist_enabled"] = _get("IP_ALLOWLIST_ENABLED", False, _parse_bool)
        kwargs["trusted_proxy"] = _get("TRUSTED_PROXY", False, _parse_bool)
        if (v := _env("EVIDENCE_PHOTOS_DIR")) is not None:
            kwargs["evidence_photos_dir"] = v
        kwargs["level_drop_cost"] = _get("LEVEL_DROP_COST", 30_000.0, float)
        kwargs["cogs_ratio"] = _get("COGS_RATIO", 0.6, float)
        if (v := _env("SKU_COSTS_PATH")) is not None:
            kwargs["sku_costs_path"] = v
        kwargs["return_shipping_cost"] = _get("RETURN_SHIPPING_COST", 120.0, float)
        kwargs["resend_shipping_cost"] = _get("RESEND_SHIPPING_COST", 120.0, float)
        kwargs["handling_cost"] = _get("HANDLING_COST", 40.0, float)
        kwargs["mediation_labor_cost"] = _get("MEDIATION_LABOR_COST", 150.0, float)
        kwargs["exchange_available"] = _get("EXCHANGE_AVAILABLE", False, _parse_bool)
        if (v := _env("STORE_NAME")) is not None:
            kwargs["store_name"] = v
        if (v := _env("SIGNATURE")) is not None:
            kwargs["signature"] = v
        kwargs["reconcile_interval_min"] = _get("RECONCILE_INTERVAL_MIN", 15, int)
        if (v := _env("DASHBOARD_USER")) is not None:
            kwargs["dashboard_user"] = v
        if (v := _env("DASHBOARD_PASSWORD")) is not None:
            kwargs["dashboard_password"] = v
        return cls(**kwargs)

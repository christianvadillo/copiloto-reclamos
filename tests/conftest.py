"""conftest.py — fixtures compartidos: API falsa de Mercado Libre + copiloto en memoria.

Nada aquí toca la red: `fake` es el simulador de `copiloto.meli.fake` servido en proceso vía
`TestClient` (mismo truco que usa `copiloto demo`), y `store` es SQLite en `:memory:`. Cada
test parte de los 7 escenarios sembrados por `seed_default_scenarios` para no repetir setup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from copiloto.app import create_app
from copiloto.config import Settings
from copiloto.meli.client import MeliClient
from copiloto.meli.fake import DEFAULT_SELLER_ID, FakeMeliState, create_fake_app, seed_default_scenarios
from copiloto.meli.oauth import TokenProvider
from copiloto.store import Store
from copiloto.worker import Worker


@pytest.fixture
def settings() -> Settings:
    return Settings(
        mode="approve",
        app_id="APPTEST",
        client_secret="test-secret",
        redirect_uri="http://localhost:8000/oauth/callback",
        llm_enabled=False,
        db_path=":memory:",
    )


@pytest.fixture
def fake(settings: Settings):
    """`(TestClient, FakeMeliState)` con los 7 escenarios de ESPECIFICACION.md §9 sembrados."""
    state = FakeMeliState(app_id=settings.app_id)
    seed_default_scenarios(state)
    fake_app, state = create_fake_app(state)
    return TestClient(fake_app), state


@pytest.fixture
def store(settings: Settings):
    s = Store(":memory:", settings.secret_key)
    yield s
    s.close()


@pytest.fixture
def seller_registered(store: Store, fake) -> str:
    """Registra el vendedor por defecto del simulador como si ya hubiera pasado por OAuth."""
    fake_client, state = fake
    access, refresh = state.issue_tokens(DEFAULT_SELLER_ID)
    store.upsert_seller(
        DEFAULT_SELLER_ID, state.seller_nickname, access, refresh, datetime.now(UTC) + timedelta(hours=3)
    )
    return DEFAULT_SELLER_ID


@pytest.fixture
def meli(settings: Settings, store: Store, fake, seller_registered: str) -> MeliClient:
    fake_client, _state = fake
    token_provider = TokenProvider(store, settings, http_client=fake_client)
    return MeliClient(settings.meli_base_url, token_provider, http_client=fake_client, sleep_fn=lambda _s: None)


@pytest.fixture
def worker(store: Store, settings: Settings, meli: MeliClient) -> Worker:
    return Worker(store, settings, meli, llm_client=None)


@pytest.fixture
def app_client(settings: Settings, store: Store, fake) -> TestClient:
    fake_client, _state = fake
    app = create_app(settings, store=store, http_client=fake_client)
    return TestClient(app)

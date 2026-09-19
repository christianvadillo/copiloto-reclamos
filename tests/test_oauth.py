"""test_oauth.py — el baile de OAuth y la rotación de tokens: el refresh token es de un solo
uso y `TokenProvider` lo persiste ANTES de devolver el access token nuevo (ver docstring de
`meli/oauth.py`). También cubre el flujo `/oauth/start` → `/oauth/callback` de `app.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from copiloto.app import create_app
from copiloto.meli.oauth import InvalidGrantError, NeedsReauthError, TokenProvider
from copiloto.meli.oauth import refresh_token as do_refresh


def test_token_provider_returns_current_token_when_not_expiring_soon(store, settings, fake, seller_registered):
    fake_client, _state = fake
    seller_before = store.get_seller(seller_registered)

    token_provider = TokenProvider(store, settings, http_client=fake_client)
    token = token_provider.get(seller_registered)

    assert token == seller_before.access_token  # no debió refrescar: faltan casi 3 h


def test_token_provider_refreshes_and_persists_rotated_refresh_token(store, settings, fake, seller_registered):
    fake_client, state = fake
    seller = store.get_seller(seller_registered)
    old_refresh = seller.refresh_token
    store.update_tokens(
        seller_registered, seller.access_token, seller.refresh_token, datetime.now(UTC) + timedelta(minutes=1)
    )

    token_provider = TokenProvider(store, settings, http_client=fake_client)
    new_access = token_provider.get(seller_registered)

    updated = store.get_seller(seller_registered)
    assert updated.access_token == new_access
    assert updated.refresh_token != old_refresh
    assert old_refresh not in state.refresh_tokens  # ya no sirve en Mercado Libre: es de un solo uso

    with pytest.raises(InvalidGrantError):
        do_refresh(fake_client, settings, old_refresh)


def test_invalid_grant_marks_seller_needs_reauth(store, settings, fake, seller_registered):
    fake_client, _state = fake
    seller = store.get_seller(seller_registered)
    store.update_tokens(
        seller_registered,
        seller.access_token,
        "refresh-que-mercado-libre-no-conoce",
        datetime.now(UTC) - timedelta(minutes=1),
    )

    token_provider = TokenProvider(store, settings, http_client=fake_client)
    with pytest.raises(NeedsReauthError):
        token_provider.get(seller_registered)

    assert store.get_seller(seller_registered).needs_reauth is True
    with pytest.raises(NeedsReauthError):
        token_provider.get(seller_registered)  # queda bloqueado hasta que re-autorice a mano


def test_oauth_start_and_callback_registers_seller(settings, store, fake):
    fake_client, state = fake
    app = create_app(settings, store=store, http_client=fake_client)
    client = TestClient(app, follow_redirects=False)

    start_resp = client.get("/oauth/start")
    assert start_resp.status_code in (302, 307)
    location = start_resp.headers["location"]
    assert location.startswith(settings.auth_base_url)
    query = parse_qs(urlparse(location).query)
    oauth_state = query["state"][0]
    assert query["code_challenge_method"] == ["S256"] and len(query["code_challenge"][0]) >= 43  # PKCE

    code = state.issue_auth_code(state.seller_id)
    callback_resp = client.get("/oauth/callback", params={"code": code, "state": oauth_state})

    assert callback_resp.status_code == 200
    seller = store.get_seller(state.seller_id)
    assert seller is not None
    assert seller.nickname == state.seller_nickname


def test_oauth_callback_rejects_unknown_state(settings, store, fake):
    fake_client, state = fake
    app = create_app(settings, store=store, http_client=fake_client)
    client = TestClient(app)
    code = state.issue_auth_code(state.seller_id)
    resp = client.get("/oauth/callback", params={"code": code, "state": "un-state-que-nadie-emitio"})
    assert resp.status_code == 400

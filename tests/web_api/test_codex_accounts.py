"""Tests for the /api/codex-accounts passthrough endpoint."""

from __future__ import annotations

import httpx
import pytest

import chat_web_api.app as app_mod
from chat_web_api.app import create_app
from chat_web_api.settings import WebApiSettings


def _settings(tmp_path) -> WebApiSettings:
    return WebApiSettings(
        sessions_dir=tmp_path / "sessions",
        web_chat_events_path=tmp_path / "web_chat" / "events.jsonl",
        pricing_cache_path=tmp_path / "pricing.json",
    )


@pytest.mark.asyncio
async def test_codex_accounts_passes_through_proxy_payload(tmp_path, monkeypatch):
    proxy_payload = {
        "accounts": [{"id": "abc", "status": "active"}],
        "models": [],
    }
    seen = {}

    async def fake_fetch(
        base_url: str, unavailable_error: str, refresh: bool = False
    ) -> tuple[int, dict]:
        seen["refresh"] = refresh
        return 200, proxy_payload

    monkeypatch.setattr(app_mod, "_fetch_proxy_usage", fake_fetch)
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/codex-accounts")
        await client.get("/api/codex-accounts?refresh=true")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["accounts"] == proxy_payload["accounts"]
    assert body["models"] == proxy_payload["models"]
    assert body["error"] is None
    # The manual-refresh query param reaches the proxy fetch.
    assert seen["refresh"] is True


@pytest.mark.asyncio
async def test_codex_accounts_reports_proxy_unavailable(tmp_path, monkeypatch):
    async def fake_fetch(
        base_url: str, unavailable_error: str, refresh: bool = False
    ) -> tuple[int, dict]:
        return 503, {"error": "codex-proxy is unavailable"}

    monkeypatch.setattr(app_mod, "_fetch_proxy_usage", fake_fetch)
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/codex-accounts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["accounts"] == []
    assert body["error"] == "codex-proxy is unavailable"


@pytest.mark.asyncio
async def test_codex_account_management_forwards_to_proxy(tmp_path, monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request(
        base_url: str,
        unavailable_error: str,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        calls.append((method, path, payload))
        return 200, {"ok": True}

    monkeypatch.setattr(app_mod, "_proxy_request", fake_request)
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/codex-accounts/tok1/promote")).status_code == 200
        assert (await client.delete("/api/codex-accounts/tok1")).status_code == 200
        assert (await client.post("/api/codex-accounts/login")).status_code == 200
        assert (await client.get("/api/codex-accounts/login/abc")).status_code == 200
        done = await client.post(
            "/api/codex-accounts/login/abc/complete", json={"value": "code#state"}
        )
        assert done.status_code == 200

    assert calls == [
        ("POST", "/tokens/tok1/promote", None),
        ("DELETE", "/tokens/tok1", None),
        ("POST", "/login", None),
        ("GET", "/login/abc", None),
        ("POST", "/login/abc/complete", {"value": "code#state"}),
    ]


@pytest.mark.asyncio
async def test_codex_account_management_propagates_proxy_errors(tmp_path, monkeypatch):
    async def fake_request(
        base_url: str,
        unavailable_error: str,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        return 404, {"error": "no token with id tok1"}

    monkeypatch.setattr(app_mod, "_proxy_request", fake_request)
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/codex-accounts/tok1/promote")

    assert resp.status_code == 404
    assert resp.json()["error"] == "no token with id tok1"

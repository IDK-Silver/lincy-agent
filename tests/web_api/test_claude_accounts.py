"""Tests for the /api/claude-accounts passthrough endpoint."""

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
async def test_claude_accounts_passes_through_proxy_payload(tmp_path, monkeypatch):
    proxy_payload = {
        "accounts": [{"id": "abc", "status": "active"}],
        "models": [{"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8"}],
    }

    async def fake_fetch(settings: WebApiSettings) -> tuple[int, dict]:
        return 200, proxy_payload

    monkeypatch.setattr(app_mod, "_fetch_claude_proxy_usage", fake_fetch)
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/claude-accounts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["accounts"] == proxy_payload["accounts"]
    assert body["models"] == proxy_payload["models"]
    assert body["error"] is None


@pytest.mark.asyncio
async def test_claude_accounts_reports_proxy_unavailable(tmp_path, monkeypatch):
    async def fake_fetch(settings: WebApiSettings) -> tuple[int, dict]:
        return 503, {"error": "claude-code-proxy is unavailable"}

    monkeypatch.setattr(app_mod, "_fetch_claude_proxy_usage", fake_fetch)
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/claude-accounts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["accounts"] == []
    assert body["error"] == "claude-code-proxy is unavailable"

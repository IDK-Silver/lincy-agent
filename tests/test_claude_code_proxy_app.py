"""Tests for inbound auth on the Claude Code proxy app.

Loopback clients are always exempt; non-loopback clients must present the
configured inbound API key and are rejected outright when no key is set.
"""

from __future__ import annotations

import json

from starlette.testclient import TestClient

from claude_code_proxy.app import create_app
from claude_code_proxy.settings import ClaudeCodeProxySettings

_BODY = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
}

_LOOPBACK = ("127.0.0.1", 55001)
_REMOTE = ("192.168.1.50", 55002)


class _AsyncResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode("utf-8")


class _AsyncClient:
    """Mock upstream httpx.AsyncClient that always succeeds."""

    def __init__(self, calls: list[dict]):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, headers: dict, json: dict):
        self._calls.append({"url": url, "headers": headers, "json": json})
        return _AsyncResponse({"content": [{"type": "text", "text": "ok"}]})


def _client(
    monkeypatch,
    *,
    api_key: str | None,
    peer: tuple[str, int] | None,
) -> tuple[TestClient, list[dict]]:
    """Return a TestClient with the given peer address plus upstream call log."""

    calls: list[dict] = []
    monkeypatch.setattr(
        "claude_code_proxy.service.httpx.AsyncClient",
        lambda timeout: _AsyncClient(calls),
    )
    settings = ClaudeCodeProxySettings(access_token="tok-upstream", api_key=api_key)
    app = create_app(settings)
    if peer is None:
        # Keep TestClient's default peer ("testclient"), which is not an IP.
        return TestClient(app), calls
    return TestClient(app, client=peer), calls


def test_loopback_client_needs_no_key(monkeypatch):
    client, calls = _client(monkeypatch, api_key=None, peer=_LOOPBACK)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "ok"
    assert len(calls) == 1


def test_loopback_client_skips_key_check_even_when_key_configured(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_LOOPBACK)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 200
    assert len(calls) == 1


def test_ipv4_mapped_loopback_counts_as_local(monkeypatch):
    client, calls = _client(
        monkeypatch, api_key="secret", peer=("::ffff:127.0.0.1", 55003)
    )

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 200
    assert len(calls) == 1


def test_remote_client_rejected_when_no_key_configured(monkeypatch):
    client, calls = _client(monkeypatch, api_key=None, peer=_REMOTE)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 401
    assert "CLAUDE_CODE_PROXY_API_KEY" in response.json()["error"]
    assert calls == []


def test_remote_client_with_valid_x_api_key(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post(
        "/v1/messages", json=_BODY, headers={"x-api-key": "secret"}
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_remote_client_with_valid_bearer_key(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post(
        "/v1/messages", json=_BODY, headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_remote_client_with_wrong_key_rejected(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post(
        "/v1/messages", json=_BODY, headers={"x-api-key": "wrong"}
    )

    assert response.status_code == 401
    assert calls == []


def test_remote_client_without_key_header_rejected(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 401
    assert calls == []


def test_unparseable_peer_treated_as_remote(monkeypatch):
    client, calls = _client(monkeypatch, api_key=None, peer=None)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 401
    assert calls == []


def test_health_stays_open_for_remote_clients(monkeypatch):
    client, _ = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

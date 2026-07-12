"""Tests for the native Claude Code proxy transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest

from claude_code_proxy.auth import StoredClaudeCodeToken, StoredClaudeCodeTokenStore
from claude_code_proxy.service import (
    EFFORT_BETA_HEADER,
    FAILURE_COOLDOWN_SECONDS,
    ClaudeCodeProxyService,
    ClaudeCodeTokenUnavailableError,
    ClaudeCodeUpstreamError,
)
from claude_code_proxy.settings import ClaudeCodeProxySettings
from chat_agent.llm.schema import ClaudeCodeRequest, ClaudeCodeMessagePayload


class _AsyncResponse:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self) -> dict:
        return self._payload


class _AsyncClient:
    """Mock httpx.AsyncClient. Effects are (status_code, payload) tuples."""

    def __init__(self, effects: list[tuple[int, dict] | Exception], calls: list[dict]):
        self._effects = effects
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, headers: dict, json: dict):
        self._calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        effect = self._effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        status, payload = effect
        return _AsyncResponse(payload, status_code=status)


def _patch_async_httpx(
    monkeypatch, effects: list[tuple[int, dict] | Exception], calls: list[dict]
) -> None:
    monkeypatch.setattr(
        "claude_code_proxy.service.httpx.AsyncClient",
        lambda timeout: _AsyncClient(effects, calls),
    )


def _point_store_at(monkeypatch, tmp_path) -> StoredClaudeCodeTokenStore:
    path = tmp_path / "tokens.json"
    monkeypatch.setattr("claude_code_proxy.auth.default_token_path", lambda: path)
    return StoredClaudeCodeTokenStore(path)


def _fresh_token(*, token_id: str, access_token: str, created_at: datetime) -> StoredClaudeCodeToken:
    return StoredClaudeCodeToken(
        id=token_id,
        access_token=access_token,
        refresh_token=None,
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        source="oauth_browser",
        client_id="client-id",
        created_at=created_at,
    )


def _expired_token(*, token_id: str, access_token: str, created_at: datetime) -> StoredClaudeCodeToken:
    return StoredClaudeCodeToken(
        id=token_id,
        access_token=access_token,
        refresh_token=None,
        expires_at=datetime.now(tz=UTC) - timedelta(hours=1),
        source="oauth_browser",
        client_id="client-id",
        created_at=created_at,
    )


def _request(model: str = "claude-sonnet-4-6") -> ClaudeCodeRequest:
    return ClaudeCodeRequest(
        model=model,
        max_tokens=4096,
        messages=[ClaudeCodeMessagePayload(role="user", content="hi")],
    )


@pytest.mark.asyncio
async def test_proxy_service_injects_required_prompt_and_preserves_cache_control(monkeypatch):
    effects = [(200, {"content": [{"type": "text", "text": "ok"}]})]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = ClaudeCodeProxyService(
        ClaudeCodeProxySettings(access_token="Bearer imported-token")
    )

    request = ClaudeCodeRequest(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[{"type": "text", "text": "[Core Rules]", "cache_control": {"type": "ephemeral"}}],
        messages=[ClaudeCodeMessagePayload(role="user", content="hi")],
    )

    body, media_type, _ = await service.forward_json(request)

    assert media_type == "application/json"
    assert json.loads(body)["content"][0]["text"] == "ok"
    payload = calls[0]["json"]
    assert payload["system"][0]["text"] == "You are Claude Code, Anthropic's official CLI for Claude."
    assert payload["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert calls[0]["headers"]["Authorization"] == "Bearer imported-token"
    assert EFFORT_BETA_HEADER in calls[0]["headers"]["anthropic-beta"].split(",")


@pytest.mark.asyncio
async def test_forward_json_merges_client_betas_without_duplicates(monkeypatch):
    effects = [(200, {"content": [{"type": "text", "text": "ok"}]})]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = ClaudeCodeProxyService(
        ClaudeCodeProxySettings(access_token="Bearer imported-token")
    )

    await service.forward_json(
        _request(),
        client_betas="context-management-2025-06-27, claude-code-20250219",
    )

    betas = calls[0]["headers"]["anthropic-beta"].split(",")
    assert "context-management-2025-06-27" in betas
    assert betas.count("claude-code-20250219") == 1


@pytest.mark.asyncio
async def test_forward_json_passes_through_ratelimit_headers(monkeypatch):
    effects = [(200, {"content": [{"type": "text", "text": "ok"}]})]
    calls: list[dict] = []
    monkeypatch.setattr(
        "claude_code_proxy.service.httpx.AsyncClient",
        lambda timeout: _AsyncClientWithHeaders(effects, calls),
    )
    service = ClaudeCodeProxyService(
        ClaudeCodeProxySettings(access_token="Bearer imported-token")
    )

    _, _, passthrough = await service.forward_json(_request())

    assert passthrough == {"anthropic-ratelimit-unified-5h-utilization": "0.42"}


class _AsyncClientWithHeaders(_AsyncClient):
    """Mock client whose responses carry rate-limit headers."""

    async def post(self, url: str, headers: dict, json: dict):
        response = await super().post(url, headers=headers, json=json)
        response.headers = {
            "content-type": "application/json",
            "anthropic-ratelimit-unified-5h-utilization": "0.42",
            "request-id": "req_x",
        }
        return response


@pytest.mark.asyncio
async def test_proxy_service_skips_effort_beta_for_non_effort_model(monkeypatch):
    effects = [(200, {"content": [{"type": "text", "text": "ok"}]})]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = ClaudeCodeProxyService(
        ClaudeCodeProxySettings(access_token="Bearer imported-token")
    )

    await service.forward_json(_request(model="claude-haiku-4-5"))

    assert EFFORT_BETA_HEADER not in calls[0]["headers"]["anthropic-beta"].split(",")


@pytest.mark.asyncio
async def test_forward_json_fails_over_to_next_token_on_401(monkeypatch, tmp_path):
    store = _point_store_at(monkeypatch, tmp_path)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    store.replace_all(
        [
            _fresh_token(token_id="primary", access_token="tok-primary", created_at=newer),
            _fresh_token(token_id="backup", access_token="tok-backup", created_at=older),
        ]
    )
    # Newest (primary) is tried first, returns 401; backup then succeeds.
    effects = [
        (401, {"error": "unauthorized"}),
        (200, {"content": [{"type": "text", "text": "ok"}]}),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)

    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())
    body, _, _ = await service.forward_json(_request())

    assert json.loads(body)["content"][0]["text"] == "ok"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok-primary"
    assert calls[1]["headers"]["Authorization"] == "Bearer tok-backup"


@pytest.mark.asyncio
async def test_forward_json_fails_over_to_next_token_on_429(monkeypatch, tmp_path):
    store = _point_store_at(monkeypatch, tmp_path)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    store.replace_all(
        [
            _fresh_token(token_id="primary", access_token="tok-primary", created_at=newer),
            _fresh_token(token_id="backup", access_token="tok-backup", created_at=older),
        ]
    )
    calls: list[dict] = []
    _patch_async_httpx(
        monkeypatch,
        [
            (429, {"error": {"type": "rate_limit_error", "message": "usage limit"}}),
            (200, {"content": [{"type": "text", "text": "ok"}]}),
        ],
        calls,
    )

    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())
    body, _, _ = await service.forward_json(_request())

    assert json.loads(body)["content"][0]["text"] == "ok"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok-primary"
    assert calls[1]["headers"]["Authorization"] == "Bearer tok-backup"


@pytest.mark.asyncio
async def test_forward_json_fails_over_to_next_token_on_read_timeout(monkeypatch, tmp_path):
    store = _point_store_at(monkeypatch, tmp_path)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    store.replace_all(
        [
            _fresh_token(token_id="primary", access_token="tok-primary", created_at=newer),
            _fresh_token(token_id="backup", access_token="tok-backup", created_at=older),
        ]
    )
    calls: list[dict] = []
    _patch_async_httpx(
        monkeypatch,
        [
            httpx.ReadTimeout("timed out while waiting for response headers"),
            (200, {"content": [{"type": "text", "text": "ok"}]}),
        ],
        calls,
    )

    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())
    body, _, _ = await service.forward_json(_request())

    assert json.loads(body)["content"][0]["text"] == "ok"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok-primary"
    assert calls[1]["headers"]["Authorization"] == "Bearer tok-backup"


@pytest.mark.asyncio
async def test_forward_json_surfaces_error_when_all_tokens_fail(monkeypatch, tmp_path):
    store = _point_store_at(monkeypatch, tmp_path)
    store.replace_all(
        [
            _fresh_token(
                token_id="a", access_token="tok-a", created_at=datetime(2026, 2, 1, tzinfo=UTC)
            ),
            _fresh_token(
                token_id="b", access_token="tok-b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            ),
        ]
    )
    effects = [(401, {"error": "unauthorized"}), (401, {"error": "unauthorized"})]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)

    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())
    with pytest.raises(ClaudeCodeUpstreamError) as excinfo:
        await service.forward_json(_request())

    assert excinfo.value.status_code == 401
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_token_manager_raises_when_no_tokens_stored(monkeypatch, tmp_path):
    _point_store_at(monkeypatch, tmp_path)
    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())
    with pytest.raises(ClaudeCodeTokenUnavailableError, match="claude-code-proxy login"):
        await service.forward_json(_request())


@pytest.mark.asyncio
async def test_401_surfaces_upstream_error_when_only_fallback_is_unusable(monkeypatch, tmp_path):
    # Primary is fresh but gets 401; the only other token is expired with no refresh,
    # so failover cannot proceed. The client must see the real 401, not a 503.
    store = _point_store_at(monkeypatch, tmp_path)
    store.replace_all(
        [
            _fresh_token(
                token_id="primary",
                access_token="tok-primary",
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            _expired_token(
                token_id="backup",
                access_token="tok-backup",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ]
    )
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, [(401, {"error": "unauthorized"})], calls)

    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())
    with pytest.raises(ClaudeCodeUpstreamError) as excinfo:
        await service.forward_json(_request())

    assert excinfo.value.status_code == 401
    assert len(calls) == 1  # backup is never called; it is unusable


@pytest.mark.asyncio
async def test_malformed_record_does_not_poison_the_store(monkeypatch, tmp_path):
    # One valid token plus a record the strict model rejects (unknown field). The
    # valid token must remain usable rather than the whole store failing to load.
    path = tmp_path / "tokens.json"
    monkeypatch.setattr("claude_code_proxy.auth.default_token_path", lambda: path)
    good = _fresh_token(
        token_id="good", access_token="tok-good", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    ).model_dump(mode="json")
    path.write_text(json.dumps([good, {"garbage": True}]))

    assert [t.id for t in StoredClaudeCodeTokenStore(path).load_all()] == ["good"]

    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, [(200, {"content": [{"type": "text", "text": "ok"}]})], calls)
    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())
    await service.forward_json(_request())
    assert calls[0]["headers"]["Authorization"] == "Bearer tok-good"


@pytest.mark.asyncio
async def test_benched_token_rejoins_pool_after_cooldown(monkeypatch, tmp_path):
    store = _point_store_at(monkeypatch, tmp_path)
    store.replace_all(
        [
            _fresh_token(
                token_id="primary",
                access_token="tok-primary",
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            _fresh_token(
                token_id="backup",
                access_token="tok-backup",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ]
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr("claude_code_proxy.service._now", lambda: clock["t"])
    service = ClaudeCodeProxyService(ClaudeCodeProxySettings())

    # Request 1: primary 401 -> benched; backup serves.
    calls1: list[dict] = []
    _patch_async_httpx(
        monkeypatch,
        [(401, {"error": "unauthorized"}), (200, {"content": [{"type": "text", "text": "a"}]})],
        calls1,
    )
    await service.forward_json(_request())
    assert calls1[-1]["headers"]["Authorization"] == "Bearer tok-backup"

    # Request 2 while primary still benched: goes straight to backup, no retry.
    calls2: list[dict] = []
    _patch_async_httpx(monkeypatch, [(200, {"content": [{"type": "text", "text": "b"}]})], calls2)
    await service.forward_json(_request())
    assert len(calls2) == 1
    assert calls2[0]["headers"]["Authorization"] == "Bearer tok-backup"

    # Advance past the cooldown: primary rejoins and reclaims top priority.
    clock["t"] += FAILURE_COOLDOWN_SECONDS + 1
    calls3: list[dict] = []
    _patch_async_httpx(monkeypatch, [(200, {"content": [{"type": "text", "text": "c"}]})], calls3)
    await service.forward_json(_request())
    assert calls3[0]["headers"]["Authorization"] == "Bearer tok-primary"

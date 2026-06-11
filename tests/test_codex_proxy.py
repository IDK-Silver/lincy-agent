"""Tests for the native Codex proxy transport."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codex_proxy.service import CodexProxyService
from codex_proxy.settings import CodexProxySettings
from chat_agent.llm.schema import (
    CodexCompactRequest,
    CodexNativeRequest,
    Message,
    ToolDefinition,
    ToolParameter,
)


def _make_fake_jwt(*, account_id: str = "acct_123", exp: int = 2_200_000_000) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "exp": exp,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }

    def _encode(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_encode(header)}.{_encode(payload)}.signature"


def _settings_with_codex_auth(tmp_path: Path, *, account_id: str) -> CodexProxySettings:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(account_id=account_id),
                    "refresh_token": "refresh-token",
                },
            }
        )
    )
    return CodexProxySettings(codex_auth_path=auth_path)


class _AsyncResponse:
    def __init__(
        self,
        text: str,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._text = text
        self.headers = headers or {"content-type": "text/event-stream"}

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> dict:
        return json.loads(self._text)


class _AsyncClient:
    def __init__(self, effects: list[_AsyncResponse], calls: list[dict]):
        self._effects = effects
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(
        self,
        url: str,
        headers: dict,
        json: dict | None = None,
        data: dict | None = None,
    ):
        call = {"method": "POST", "url": url, "headers": headers}
        if json is not None:
            call["json"] = json
        if data is not None:
            call["data"] = data
        self._calls.append(call)
        return self._effects.pop(0)


def _patch_async_httpx(monkeypatch, effects: list[_AsyncResponse], calls: list[dict]) -> None:
    monkeypatch.setattr(
        "codex_proxy.service.httpx.AsyncClient",
        lambda timeout: _AsyncClient(effects, calls),
    )


@pytest.mark.asyncio
async def test_proxy_service_calls_upstream_and_parses_text(monkeypatch, tmp_path: Path):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hello from codex"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18,"input_tokens_details":{"cached_tokens":3}}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_proxy"))

    request = CodexNativeRequest(
        model="gpt-5.2-codex",
        messages=[
            Message(role="system", content="You are helpful."),
            Message(role="user", content="hi"),
        ],
    )

    response = await service.chat(request)

    assert response.content == "hello from codex"
    assert response.prompt_tokens == 11
    assert response.cache_read_tokens == 3
    assert calls[0]["url"].endswith("/codex/responses")
    assert calls[0]["headers"]["chatgpt-account-id"] == "acct_proxy"
    assert calls[0]["json"]["instructions"] == "You are helpful."


@pytest.mark.asyncio
async def test_proxy_service_refreshes_expired_official_codex_auth(
    monkeypatch,
    tmp_path: Path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(
                        account_id="acct_expired",
                        exp=1_700_000_000,
                    ),
                    "refresh_token": "refresh-expired",
                },
            }
        )
    )
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [
        _AsyncResponse(
            json.dumps(
                {
                    "access_token": _make_fake_jwt(account_id="acct_refreshed"),
                    "refresh_token": "refresh-next",
                }
            ),
            headers={"content-type": "application/json"},
        ),
        _AsyncResponse(sse),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(CodexProxySettings(codex_auth_path=auth_path))

    response = await service.chat(
        CodexNativeRequest(
            model="gpt-5.4",
            messages=[Message(role="user", content="hi")],
        )
    )

    assert response.content == "ok"
    assert calls[0]["url"] == "https://auth.openai.com/oauth/token"
    assert calls[0]["data"]["refresh_token"] == "refresh-expired"
    assert calls[1]["headers"]["chatgpt-account-id"] == "acct_refreshed"


@pytest.mark.asyncio
async def test_proxy_service_translates_tools_and_response_schema(
    monkeypatch,
    tmp_path: Path,
):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_tools"))
    request = CodexNativeRequest(
        model="gpt-5.2-codex",
        messages=[Message(role="user", content="hi")],
        tools=[
            ToolDefinition(
                name="read_file",
                description="read file",
                parameters={"path": ToolParameter(type="string", description="path")},
                required=["path"],
            )
        ],
        response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        reasoning_effort="medium",
    )

    response = await service.chat(request)

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    payload = calls[0]["json"]
    assert payload["tools"][0]["name"] == "read_file"
    assert payload["text"]["format"]["schema"]["type"] == "object"
    assert payload["reasoning"]["effort"] == "medium"


@pytest.mark.asyncio
async def test_proxy_service_drops_max_output_tokens_for_chatgpt_backend(
    monkeypatch,
    tmp_path: Path,
):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_tokens"))
    request = CodexNativeRequest(
        model="gpt-5.4",
        messages=[Message(role="user", content="hi")],
        max_output_tokens=64,
    )

    response = await service.chat(request)

    assert response.content == "ok"
    assert "max_output_tokens" not in calls[0]["json"]


@pytest.mark.asyncio
async def test_proxy_service_forwards_prompt_cache_key(monkeypatch, tmp_path: Path):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_cache"))
    request = CodexNativeRequest(
        model="gpt-5.4",
        messages=[Message(role="user", content="hi")],
        prompt_cache_key="session-1:brain:20260411",
    )

    response = await service.chat(request)

    assert response.content == "ok"
    assert calls[0]["json"]["prompt_cache_key"] == "session-1:brain:20260411"


@pytest.mark.asyncio
async def test_proxy_service_replays_turn_state_within_same_turn(
    monkeypatch,
    tmp_path: Path,
):
    sse_first = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    sse_second = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":7,"output_tokens":1,"total_tokens":8}}}',
        ]
    )
    effects = [
        _AsyncResponse(
            sse_first,
            headers={
                "content-type": "text/event-stream",
                "x-codex-turn-state": "ts-1",
            },
        ),
        _AsyncResponse(sse_second),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(
        _settings_with_codex_auth(tmp_path, account_id="acct_turn_state")
    )

    first = await service.chat(
        CodexNativeRequest(
            model="gpt-5.4",
            messages=[Message(role="user", content="hi")],
            session_id="20260411_abcdef",
            turn_id="turn_000123",
        )
    )
    second = await service.chat(
        CodexNativeRequest(
            model="gpt-5.4",
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content=None, tool_calls=first.tool_calls),
                Message(
                    role="tool",
                    tool_call_id="call_1",
                    name="read_file",
                    content="README contents",
                ),
            ],
            session_id="20260411_abcdef",
            turn_id="turn_000123",
        )
    )

    assert second.content == "done"
    assert "x-codex-turn-state" not in calls[0]["headers"]
    assert calls[0]["headers"]["session_id"] == "20260411_abcdef"
    assert calls[1]["headers"]["x-codex-turn-state"] == "ts-1"
    assert calls[1]["headers"]["session_id"] == "20260411_abcdef"
    assert json.loads(calls[1]["headers"]["x-codex-turn-metadata"]) == {
        "turn_id": "turn_000123"
    }


@pytest.mark.asyncio
async def test_proxy_service_calls_compact_endpoint_and_maps_compaction_items(
    monkeypatch,
    tmp_path: Path,
):
    effects = [
        _AsyncResponse(
            json.dumps(
                {
                    "output": [
                        {
                            "type": "compaction_summary",
                            "encrypted_content": "enc_123",
                        }
                    ]
                }
            ),
            headers={"content-type": "application/json"},
        )
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_compact"))

    response = await service.compact(
        CodexCompactRequest(
            model="gpt-5.4",
            messages=[
                Message(role="system", content="You are helpful."),
                Message(role="user", content="hi"),
            ],
        )
    )

    assert calls[0]["url"].endswith("/codex/responses/compact")
    assert calls[0]["json"]["instructions"] == "You are helpful."
    assert response.messages[0].codex_compaction_encrypted_content == "enc_123"

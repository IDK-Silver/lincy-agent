"""Tests for Codex proxy auth loading."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from codex_proxy.auth import (
    CodexAuthLoader,
    default_codex_auth_path,
    extract_chatgpt_account_id,
)
from codex_proxy.settings import CodexProxySettings


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


def test_codex_auth_loader_reads_official_auth_json(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    access_token = _make_fake_jwt(account_id="acct_loader")
    auth_path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": "refresh-loader",
                },
            }
        )
    )

    loaded = CodexAuthLoader(path=auth_path).load()

    assert loaded is not None
    assert loaded.account_id == "acct_loader"
    assert loaded.refresh_token == "refresh-loader"
    assert loaded.source == "codex_auth"
    assert extract_chatgpt_account_id(loaded.access_token) == "acct_loader"


def test_settings_from_env_uses_default_codex_auth_path(monkeypatch, tmp_path: Path):
    custom_auth_path = tmp_path / "auth.json"
    custom_token_path = tmp_path / "token.json"
    monkeypatch.setenv("CODEX_PROXY_CODEX_AUTH_PATH", str(custom_auth_path))
    monkeypatch.setenv("CODEX_PROXY_TOKEN_PATH", str(custom_token_path))
    monkeypatch.setenv("CODEX_PROXY_ENABLE_CODEX_AUTH_FALLBACK", "1")
    monkeypatch.setenv("CODEX_PROXY_ACCESS_TOKEN", _make_fake_jwt())

    settings = CodexProxySettings.from_env()

    assert settings.codex_auth_path == default_codex_auth_path()
    assert settings.codex_auth_path != custom_auth_path

"""Environment-backed settings for the native Codex proxy."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from .auth import (
    DEFAULT_CODEX_OAUTH_CLIENT_ID,
    DEFAULT_CODEX_OAUTH_TOKEN_URL,
    resolve_codex_auth_path,
)


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class CodexProxySettings:
    codex_auth_path: Path = field(default_factory=resolve_codex_auth_path)
    host: str = "127.0.0.1"
    port: int = 4143
    request_timeout: float = 120.0
    codex_base_url: str = "https://chatgpt.com/backend-api"
    oauth_token_url: str = DEFAULT_CODEX_OAUTH_TOKEN_URL
    oauth_client_id: str = DEFAULT_CODEX_OAUTH_CLIENT_ID

    @classmethod
    def from_env(cls) -> "CodexProxySettings":
        codex_base_url = (_env("CODEX_PROXY_BASE_URL") or "https://chatgpt.com/backend-api").rstrip("/")
        oauth_token_url = _env("CODEX_PROXY_TOKEN_URL") or DEFAULT_CODEX_OAUTH_TOKEN_URL
        host = _env("CODEX_PROXY_HOST") or "127.0.0.1"
        port = int(_env("CODEX_PROXY_PORT") or "4143")
        request_timeout = float(_env("CODEX_PROXY_REQUEST_TIMEOUT") or "120")
        oauth_client_id = _env("CODEX_PROXY_CLIENT_ID") or DEFAULT_CODEX_OAUTH_CLIENT_ID
        return cls(
            host=host,
            port=port,
            request_timeout=request_timeout,
            codex_base_url=codex_base_url,
            oauth_token_url=oauth_token_url,
            oauth_client_id=oauth_client_id,
        )

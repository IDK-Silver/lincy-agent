"""Environment-backed settings for the native Codex proxy."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from .auth import (
    DEFAULT_CODEX_OAUTH_AUTHORIZE_URL,
    DEFAULT_CODEX_OAUTH_CLIENT_ID,
    DEFAULT_CODEX_OAUTH_REDIRECT_URI,
    DEFAULT_CODEX_OAUTH_SCOPE,
    DEFAULT_CODEX_OAUTH_TOKEN_URL,
    default_token_path,
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
    token_path: Path = field(default_factory=default_token_path)
    host: str = "127.0.0.1"
    port: int = 4143
    request_timeout: float = 120.0
    codex_base_url: str = "https://chatgpt.com/backend-api"
    oauth_authorize_url: str = DEFAULT_CODEX_OAUTH_AUTHORIZE_URL
    oauth_token_url: str = DEFAULT_CODEX_OAUTH_TOKEN_URL
    oauth_redirect_uri: str = DEFAULT_CODEX_OAUTH_REDIRECT_URI
    oauth_client_id: str = DEFAULT_CODEX_OAUTH_CLIENT_ID
    oauth_scope: str = DEFAULT_CODEX_OAUTH_SCOPE
    # Local listener for the browser OAuth redirect. Defaults match
    # oauth_redirect_uri (port 1455 is registered with the OAuth client and
    # cannot move); tests override the port to 0 to bind an ephemeral port.
    callback_bind_host: str = "127.0.0.1"
    callback_bind_port: int = 1455
    # Inbound key required from non-loopback clients on the management surface
    # (/usage, /login*, /tokens*); loopback never needs it. Distinct from the
    # upstream Codex OAuth credentials.
    api_key: str | None = None
    # chatgpt.com's edge rejects default python HTTP client UAs with a 403 HTML
    # challenge on /codex/usage (verified 2026-07-18); mimic the official CLI.
    user_agent: str = "codex_cli_rs/0.144.3"

    @classmethod
    def from_env(cls) -> "CodexProxySettings":
        settings = cls.for_login_from_env()
        codex_base_url = (
            _env("CODEX_PROXY_BASE_URL") or "https://chatgpt.com/backend-api"
        ).rstrip("/")
        api_key = _env("CODEX_PROXY_API_KEY")
        user_agent = _env("CODEX_PROXY_USER_AGENT") or cls.user_agent
        return cls(
            codex_auth_path=settings.codex_auth_path,
            token_path=settings.token_path,
            host=settings.host,
            port=settings.port,
            request_timeout=settings.request_timeout,
            codex_base_url=codex_base_url,
            oauth_authorize_url=settings.oauth_authorize_url,
            oauth_token_url=settings.oauth_token_url,
            oauth_redirect_uri=settings.oauth_redirect_uri,
            oauth_client_id=settings.oauth_client_id,
            oauth_scope=settings.oauth_scope,
            callback_bind_host=settings.callback_bind_host,
            callback_bind_port=settings.callback_bind_port,
            api_key=api_key,
            user_agent=user_agent,
        )

    @classmethod
    def for_login_from_env(cls) -> "CodexProxySettings":
        host = _env("CODEX_PROXY_HOST") or "127.0.0.1"
        port = int(_env("CODEX_PROXY_PORT") or "4143")
        request_timeout = float(_env("CODEX_PROXY_REQUEST_TIMEOUT") or "120")
        token_path_override = _env("CODEX_PROXY_TOKEN_PATH")
        token_path = Path(token_path_override).expanduser() if token_path_override else default_token_path()
        oauth_authorize_url = _env("CODEX_PROXY_AUTHORIZE_URL") or DEFAULT_CODEX_OAUTH_AUTHORIZE_URL
        oauth_token_url = _env("CODEX_PROXY_TOKEN_URL") or DEFAULT_CODEX_OAUTH_TOKEN_URL
        oauth_redirect_uri = _env("CODEX_PROXY_REDIRECT_URI") or DEFAULT_CODEX_OAUTH_REDIRECT_URI
        oauth_client_id = _env("CODEX_PROXY_CLIENT_ID") or DEFAULT_CODEX_OAUTH_CLIENT_ID
        oauth_scope = _env("CODEX_PROXY_SCOPE") or DEFAULT_CODEX_OAUTH_SCOPE
        callback_bind_port = int(_env("CODEX_PROXY_CALLBACK_PORT") or "1455")
        return cls(
            token_path=token_path,
            host=host,
            port=port,
            request_timeout=request_timeout,
            oauth_authorize_url=oauth_authorize_url,
            oauth_token_url=oauth_token_url,
            oauth_redirect_uri=oauth_redirect_uri,
            oauth_client_id=oauth_client_id,
            oauth_scope=oauth_scope,
            callback_bind_port=callback_bind_port,
        )

"""Auth helpers for the native Codex proxy."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Reverse-engineered from the official Codex CLI OAuth state.
DEFAULT_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoredCodexToken(_StrictModel):
    """Codex OAuth token loaded from the official CLI auth file."""

    version: int = 1
    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    account_id: str = Field(min_length=1)
    expires_at: datetime
    source: Literal["codex_auth", "oauth_refresh"]
    client_id: str = Field(min_length=1)
    created_at: datetime


class CodexAuthPayload(BaseModel):
    """Subset of the official Codex CLI auth.json payload."""

    auth_mode: str | None = None
    last_refresh: str | int | float | None = None
    tokens: dict[str, Any]

    model_config = ConfigDict(extra="ignore")


def default_codex_auth_path() -> Path:
    """Return the default official Codex CLI auth path."""

    return Path.home() / ".codex" / "auth.json"


def resolve_codex_auth_path(path: str | Path | None = None) -> Path:
    if path is None:
        return default_codex_auth_path()
    return Path(path).expanduser()


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("token is not a JWT")
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        raw = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("failed to decode JWT payload") from exc
    if not isinstance(raw, dict):
        raise ValueError("JWT payload must be an object")
    return raw


def extract_token_expiry(token: str) -> datetime:
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise ValueError("JWT payload missing exp")
    return datetime.fromtimestamp(float(exp), tz=UTC)


def extract_chatgpt_account_id(token: str) -> str:
    payload = _decode_jwt_payload(token)
    auth_info = payload.get("https://api.openai.com/auth")
    if not isinstance(auth_info, dict):
        raise ValueError("JWT payload missing OpenAI auth claim")
    account_id = auth_info.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("JWT payload missing chatgpt_account_id")
    return account_id


def is_token_fresh(token: StoredCodexToken, *, skew_seconds: int = 60) -> bool:
    return token.expires_at.timestamp() - skew_seconds > datetime.now(tz=UTC).timestamp()


def _parse_created_at(value: str | int | float | None) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(tz=UTC)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(tz=UTC)


class CodexAuthLoader:
    """Read official Codex CLI auth state without modifying it."""

    def __init__(self, *, path: Path | None = None):
        self._path = path

    def load(self) -> StoredCodexToken | None:
        path = self._path or default_codex_auth_path()
        if not path.is_file():
            return None
        try:
            payload = CodexAuthPayload.model_validate_json(path.read_text())
        except (OSError, ValidationError, ValueError) as exc:
            raise ValueError(f"Failed to parse Codex auth at {path}: {exc}") from exc

        tokens = payload.tokens
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        account_id = tokens.get("account_id")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError(f"Codex auth at {path} does not contain access_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise ValueError(f"Codex auth at {path} has invalid refresh_token")
        if not isinstance(account_id, str) or not account_id:
            account_id = extract_chatgpt_account_id(access_token)

        return StoredCodexToken(
            access_token=access_token,
            refresh_token=refresh_token,
            account_id=account_id,
            expires_at=extract_token_expiry(access_token),
            source="codex_auth",
            client_id=DEFAULT_CODEX_OAUTH_CLIENT_ID,
            created_at=_parse_created_at(payload.last_refresh),
        )

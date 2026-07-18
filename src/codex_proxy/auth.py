"""Auth helpers for the native Codex proxy."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import sys
import threading
from typing import Any, Callable, Iterator, Literal
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Reverse-engineered from:
# https://github.com/insightflo/chatgpt-codex-proxy/blob/main/src/auth.ts
# https://github.com/icebear0828/codex-proxy/blob/main/src/auth/oauth-pkce.ts
DEFAULT_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_CODEX_OAUTH_SCOPE = "openid profile email offline_access"
DEFAULT_CODEX_OAUTH_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
DEFAULT_CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CODEX_OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"

# Fixed id for the implicit pool entry backed by the official Codex CLI auth
# file. There is only ever one such file, so a stable sentinel (rather than a
# generated id) lets callers refer to it consistently across restarts.
CODEX_AUTH_FALLBACK_TOKEN_ID = "__codex_auth__"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoredCodexToken(_StrictModel):
    """Persisted Codex OAuth token used by the local proxy."""

    version: int = 1
    id: str = Field(min_length=1)
    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    account_id: str = Field(min_length=1)
    expires_at: datetime
    source: Literal["codex_auth", "oauth_browser", "oauth_refresh"]
    client_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("expires_at", "created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        # Externally-produced or hand-edited records may store naive datetimes; assume
        # UTC so sorting by created_at and expires_at.timestamp() never mix naive/aware.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class CodexAuthPayload(BaseModel):
    """Subset of the official Codex CLI auth.json payload."""

    auth_mode: str | None = None
    last_refresh: str | int | float | None = None
    tokens: dict[str, Any]

    model_config = ConfigDict(extra="ignore")


class CodexOAuthTokens(_StrictModel):
    """OAuth token payload returned by auth.openai.com."""

    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    expires_in: int | float | None = None

    model_config = ConfigDict(extra="ignore")


class CodexBrowserAuthorization(_StrictModel):
    """Browser authorization metadata for the Codex OAuth flow."""

    authorization_url: str = Field(min_length=1)
    code_verifier: str = Field(min_length=1)
    state: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)


def default_token_path() -> Path:
    """Return the platform-appropriate default proxy token store path."""

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "chat-agent" / "codex-proxy" / "tokens.json"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "chat-agent"
            / "codex-proxy"
            / "tokens.json"
        )
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "chat-agent" / "codex-proxy" / "tokens.json"


def default_codex_auth_path() -> Path:
    """Return the default official Codex CLI auth path."""

    return Path.home() / ".codex" / "auth.json"


def resolve_token_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is None:
        return default_token_path()
    return Path(path).expanduser()


def resolve_codex_auth_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is None:
        return default_codex_auth_path()
    return Path(path).expanduser()


def build_pkce_pair() -> tuple[str, str]:
    """Return a PKCE verifier/challenge pair."""

    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def build_state_token() -> str:
    """Return a CSRF state token for the browser OAuth flow."""

    return secrets.token_urlsafe(24)


def new_token_id() -> str:
    """Return a short opaque id for a stored token."""

    return secrets.token_urlsafe(8)


def normalize_bearer_token(value: str) -> str:
    cleaned = value.strip()
    if cleaned.lower().startswith("bearer "):
        return cleaned[7:].strip()
    return cleaned


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


def parse_manual_callback_value(value: str) -> tuple[str, str]:
    """Parse a pasted OAuth callback into (code, state).

    Accepts either the full callback URL (as shown by the local listener or
    copied from the browser address bar) or the bare `code#state` pair, for
    the case where the browser could not reach the local callback listener.
    """

    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Paste the callback URL or `code#state` from the Codex callback page.")
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        parsed = urlparse(cleaned)
        code, state, error = parse_callback_query(parsed.query)
        if error:
            raise ValueError(f"Codex callback reported an error: {error}")
        if not code or not state:
            raise ValueError("Callback URL is missing the code or state parameter.")
        return code, state
    code, separator, state = cleaned.partition("#")
    if not separator or not code or not state:
        raise ValueError("Paste the full callback URL or `code#state` from the Codex callback page.")
    return code.strip(), state.strip()


def parse_callback_query(query: str) -> tuple[str | None, str | None, str | None]:
    """Return (code, state, error) parsed from a callback query string.

    Shared by the blocking CLI callback server and the async serve-mode
    listener so both interpret the redirect the same way.
    """

    params = parse_qs(query)
    code = _first_query_value(params, "code")
    state = _first_query_value(params, "state")
    error = _first_query_value(params, "error") or _first_query_value(params, "error_description")
    return code, state, error


def _first_query_value(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def render_callback_html(*, success: bool, message: str | None) -> str:
    """Minimal ASCII HTML shown in the browser tab after the OAuth redirect.

    Shared by the blocking CLI listener and the async serve-mode listener.
    """

    body = "Codex login complete. You can close this tab." if success else (
        message or "Codex login failed. Check the terminal and try again."
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Codex Login</title></head><body><p>"
        f"{body}"
        "</p></body></html>"
    )


class StoredCodexTokenStore:
    """Load and save proxy-managed Codex tokens (multi-token store)."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_token_path()

    def load_all(self) -> list[StoredCodexToken]:
        """Return all stored tokens ordered by priority (newest created_at first)."""

        tokens = self._read_records()
        if tokens is None:
            return []
        return sorted(tokens, key=lambda t: t.created_at, reverse=True)

    def save(self, token: StoredCodexToken) -> None:
        """Append a token to the store, keeping existing entries."""

        with self._exclusive_lock():
            tokens = [t for t in self._read_records() or [] if t.id != token.id]
            tokens.append(token)
            self._write_records(tokens)

    def replace_all(self, tokens: list[StoredCodexToken]) -> None:
        with self._exclusive_lock():
            self._write_records(tokens)

    def remove(self, token_id: str) -> bool:
        """Drop the token with the given id. Return True if a token was removed."""

        with self._exclusive_lock():
            tokens = self._read_records() or []
            remaining = [t for t in tokens if t.id != token_id]
            if len(remaining) == len(tokens):
                return False
            self._write_records(remaining)
            return True

    def promote(self, token_id: str) -> bool:
        """Move the token with the given id to the front of the priority order.

        Priority is by ``created_at`` (newest first). Promoting means bumping its
        ``created_at`` to just above the current maximum so it sorts first.
        Return True if the token existed.
        """

        with self._exclusive_lock():
            tokens = self._read_records() or []
            target = next((t for t in tokens if t.id == token_id), None)
            if target is None:
                return False
            max_created = max((t.created_at for t in tokens), default=datetime.now(tz=UTC))
            bumped = target.model_copy(
                update={"created_at": max_created.replace(microsecond=0) + timedelta(seconds=1)}
            )
            new_tokens = [bumped if t.id == token_id else t for t in tokens]
            self._write_records(new_tokens)
            return True

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Serialize read-modify-write across processes via an advisory file lock.

        Guards against a lost update when serve's in-process token refresh and a
        separate `login` / `tokens` CLI invocation write the store concurrently.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read_records(self) -> list[StoredCodexToken] | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"Invalid Codex token store at {self.path}: {exc}") from exc
        if not isinstance(payload, (list, dict)):
            raise ValueError(f"Invalid Codex token store at {self.path}: unexpected payload")
        return self._parse_records(payload)

    @staticmethod
    def _parse_records(payload: object) -> list[StoredCodexToken]:
        """Validate records, skipping any malformed one so it can't poison the store.

        A single bad record (hand-edited, truncated, or written by a build with an
        extra field) must not disable every other stored token and defeat failover.
        """

        items = payload if isinstance(payload, list) else [payload]
        records: list[StoredCodexToken] = []
        for item in items:
            try:
                records.append(StoredCodexToken.model_validate(item))
            except ValidationError:
                continue
        return records

    def _write_records(self, tokens: list[StoredCodexToken]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps([t.model_dump(mode="json") for t in tokens], indent=2))
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        temp_path.replace(self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)


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
            id=CODEX_AUTH_FALLBACK_TOKEN_ID,
            access_token=access_token,
            refresh_token=refresh_token,
            account_id=account_id,
            expires_at=extract_token_expiry(access_token),
            source="codex_auth",
            client_id=DEFAULT_CODEX_OAUTH_CLIENT_ID,
            created_at=_parse_created_at(payload.last_refresh),
        )


class CodexOAuthClient:
    """Run Codex browser OAuth and persist resulting proxy tokens."""

    def __init__(
        self,
        *,
        request_timeout: float,
        client_id: str = DEFAULT_CODEX_OAUTH_CLIENT_ID,
        authorize_url: str = DEFAULT_CODEX_OAUTH_AUTHORIZE_URL,
        token_url: str = DEFAULT_CODEX_OAUTH_TOKEN_URL,
        redirect_uri: str = DEFAULT_CODEX_OAUTH_REDIRECT_URI,
        scope: str = DEFAULT_CODEX_OAUTH_SCOPE,
    ):
        self._request_timeout = request_timeout
        self._client_id = client_id
        self._authorize_url = authorize_url.rstrip("/")
        self._token_url = token_url
        self._redirect_uri = redirect_uri
        self._scope = scope

    def begin_authorization(self) -> CodexBrowserAuthorization:
        code_verifier, code_challenge = build_pkce_pair()
        state = build_state_token()
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": self._scope,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "state": state,
                "originator": "codex_cli_rs",
            },
            quote_via=quote,
        )
        return CodexBrowserAuthorization(
            authorization_url=f"{self._authorize_url}?{params}",
            code_verifier=code_verifier,
            state=state,
            redirect_uri=self._redirect_uri,
        )

    def exchange_callback_code(
        self,
        code: str,
        *,
        returned_state: str,
        authorization: CodexBrowserAuthorization,
    ) -> StoredCodexToken:
        if returned_state != authorization.state:
            raise ValueError("Authorization state mismatch. Restart the login flow.")

        with httpx.Client(timeout=self._request_timeout) as client:
            response = client.post(
                self._token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._client_id,
                    "code": code,
                    "code_verifier": authorization.code_verifier,
                    "redirect_uri": authorization.redirect_uri,
                },
            )
        if response.status_code >= 400:
            detail = response.text.strip() or f"HTTP {response.status_code}"
            raise RuntimeError(f"Codex OAuth token exchange failed: {detail}")
        try:
            payload = CodexOAuthTokens.model_validate(response.json())
        except ValidationError as exc:
            raise RuntimeError(
                f"Codex OAuth token exchange returned invalid payload: {exc}"
            ) from exc
        return self.build_stored_token(payload)

    def build_stored_token(self, payload: CodexOAuthTokens) -> StoredCodexToken:
        return StoredCodexToken(
            id=new_token_id(),
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
            account_id=extract_chatgpt_account_id(payload.access_token),
            expires_at=extract_token_expiry(payload.access_token),
            source="oauth_browser",
            client_id=self._client_id,
            created_at=datetime.now(tz=UTC),
        )


def wait_for_browser_callback(
    authorization: CodexBrowserAuthorization,
    *,
    on_ready: Callable[[], None] | None = None,
    timeout_seconds: float = 300.0,
) -> tuple[str, str]:
    """Block until the browser OAuth callback arrives and return (code, state).

    Used by the blocking `proxy codex login` CLI flow. The serve-mode service
    runs a non-blocking counterpart (see codex_proxy.service) that shares the
    query-parsing and HTML-rendering helpers above.
    """

    parsed = urlparse(authorization.redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    callback_path = parsed.path or "/"
    results: queue.Queue[tuple[str | None, str | None, str | None]] = queue.Queue(maxsize=1)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request_url = urlparse(self.path)
            if request_url.path != callback_path:
                self.send_error(404, "Not found")
                return

            code, state, error = parse_callback_query(request_url.query)
            if error:
                results.put((None, None, error))
                self._write_html(render_callback_html(success=False, message=error))
                return
            if not code or not state:
                results.put((None, None, "Missing code or state parameter"))
                self._write_html(
                    render_callback_html(
                        success=False,
                        message="Missing code or state parameter.",
                    )
                )
                return

            results.put((code, state, None))
            self._write_html(render_callback_html(success=True, message=None))

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _write_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    try:
        server = HTTPServer((host, port), _Handler)
    except OSError as exc:
        raise RuntimeError(
            f"Failed to start OAuth callback server on {host}:{port}: {exc}"
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if on_ready is not None:
        on_ready()
    try:
        code, state, error = results.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise RuntimeError("Timed out waiting for browser OAuth callback.") from exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)

    if error:
        raise RuntimeError(f"Browser OAuth failed: {error}")
    assert code is not None
    assert state is not None
    return code, state

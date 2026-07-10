"""xAI SuperGrok OAuth device-flow helpers for the native Grok proxy."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Iterator, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_GROK_OAUTH_ISSUER = "https://auth.x.ai"
DEFAULT_GROK_OAUTH_DISCOVERY_URL = f"{DEFAULT_GROK_OAUTH_ISSUER}/.well-known/openid-configuration"
DEFAULT_GROK_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_GROK_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEFAULT_GROK_OAUTH_DEVICE_CODE_URL = f"{DEFAULT_GROK_OAUTH_ISSUER}/oauth2/device/code"
DEFAULT_GROK_OAUTH_TOKEN_URL = f"{DEFAULT_GROK_OAUTH_ISSUER}/oauth2/token"
DEFAULT_GROK_API_BASE_URL = "https://api.x.ai/v1"

# Access tokens from SuperGrok OAuth are short-lived (~6h). Refresh early so
# heartbeat/cron gaps do not hit a near-expired token.
DEFAULT_REFRESH_SKEW_SECONDS = 3600


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoredGrokToken(_StrictModel):
    """Persisted SuperGrok OAuth token used by the local proxy."""

    version: int = 1
    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    expires_at: datetime | None = None
    token_type: str = "Bearer"
    client_id: str = Field(min_length=1)
    token_endpoint: str = Field(min_length=1)
    source: Literal["oauth_device_code", "oauth_refresh"]
    created_at: datetime
    updated_at: datetime

    @field_validator("expires_at", "created_at", "updated_at")
    @classmethod
    def _ensure_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class GrokDeviceCode(_StrictModel):
    """Device-flow verification payload returned by xAI."""

    device_code: str = Field(min_length=1)
    user_code: str = Field(min_length=1)
    verification_uri: str = Field(min_length=1)
    verification_uri_complete: str | None = None
    expires_in: int = Field(ge=1)
    interval: int = Field(default=5, ge=1)


class GrokOAuthTokens(_StrictModel):
    """Token payload returned by the xAI token endpoint.

    Device-code grants must include refresh_token. Refresh grants may omit it
    and reuse the previous refresh token.
    """

    model_config = ConfigDict(extra="ignore")

    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int | float | None = None
    id_token: str | None = None


class GrokOAuthError(_StrictModel):
    """Structured error payload returned by xAI OAuth endpoints."""

    model_config = ConfigDict(extra="ignore")

    error: str
    error_description: str | None = None


class GrokOAuthDiscovery(_StrictModel):
    device_authorization_endpoint: str = Field(min_length=1)
    token_endpoint: str = Field(min_length=1)


class DeviceAuthorizationError(RuntimeError):
    """Raised when the device flow cannot complete."""


class GrokAuthError(RuntimeError):
    """Raised when stored credentials cannot be loaded or refreshed."""


def default_token_path() -> Path:
    """Return the platform-appropriate default token store location."""

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "chat-agent" / "grok-proxy" / "token.json"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "chat-agent"
            / "grok-proxy"
            / "token.json"
        )
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "chat-agent" / "grok-proxy" / "token.json"


def resolve_token_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve and expand the token path override if provided."""

    if path is None:
        return default_token_path()
    return Path(path).expanduser()


def is_trusted_xai_oauth_endpoint(url: str) -> bool:
    """Return True when the endpoint is an https x.ai host."""

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "x.ai" or host.endswith(".x.ai")


def require_trusted_xai_oauth_endpoint(url: str, *, field: str) -> str:
    cleaned = url.strip()
    if not cleaned or not is_trusted_xai_oauth_endpoint(cleaned):
        raise GrokAuthError(f"Untrusted xAI OAuth {field}: {url!r}")
    return cleaned


def decode_jwt_exp(token: str) -> datetime | None:
    """Best-effort JWT exp claim extraction; returns None for opaque tokens."""

    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        raw = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    exp = raw.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(float(exp), tz=UTC)


def resolve_expires_at(
    *,
    access_token: str,
    expires_in: int | float | None,
    now: datetime | None = None,
) -> datetime | None:
    """Prefer RFC 6749 expires_in; fall back to access-token JWT exp."""

    current = now or datetime.now(tz=UTC)
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return current + timedelta(seconds=float(expires_in))
    return decode_jwt_exp(access_token)


def is_token_fresh(
    token: StoredGrokToken,
    *,
    skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Return True when the access token is usable without refresh."""

    current = now or datetime.now(tz=UTC)
    if token.expires_at is None:
        exp = decode_jwt_exp(token.access_token)
        if exp is None:
            # Opaque token with no expiry: treat as usable until a 401 forces refresh.
            return True
        return exp.timestamp() - skew_seconds > current.timestamp()
    return token.expires_at.timestamp() - skew_seconds > current.timestamp()


class GrokTokenStore:
    """Load and save SuperGrok OAuth tokens for the local proxy."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_token_path()

    def load(self) -> StoredGrokToken | None:
        if not self.path.is_file():
            return None
        try:
            return StoredGrokToken.model_validate_json(self.path.read_text())
        except (OSError, ValidationError) as exc:
            raise GrokAuthError(f"Invalid Grok token store at {self.path}: {exc}") from exc

    def save(self, token: StoredGrokToken) -> None:
        with self._exclusive_lock():
            self._write(token)

    def update(self, token: StoredGrokToken) -> None:
        """Replace the stored token under the same lock used for refresh."""

        with self._exclusive_lock():
            self._write(token)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write(self, token: StoredGrokToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(token.model_dump_json(indent=2))
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        temp_path.replace(self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)


class GrokOAuthClient:
    """Run xAI OAuth device flow and refresh SuperGrok tokens."""

    def __init__(
        self,
        *,
        request_timeout: float,
        client_id: str = DEFAULT_GROK_OAUTH_CLIENT_ID,
        scope: str = DEFAULT_GROK_OAUTH_SCOPE,
        discovery_url: str = DEFAULT_GROK_OAUTH_DISCOVERY_URL,
        device_code_url: str = DEFAULT_GROK_OAUTH_DEVICE_CODE_URL,
        token_url: str = DEFAULT_GROK_OAUTH_TOKEN_URL,
    ):
        self._request_timeout = request_timeout
        self._client_id = client_id
        self._scope = scope
        self._discovery_url = discovery_url
        self._device_code_url = device_code_url
        self._token_url = token_url

    def discover(self) -> GrokOAuthDiscovery:
        """Fetch OIDC discovery, falling back to known endpoints on failure."""

        try:
            with httpx.Client(timeout=self._request_timeout) as client:
                response = client.get(
                    self._discovery_url,
                    headers={"Accept": "application/json"},
                )
            if response.status_code == 200:
                payload = self._parse_json(response)
                device = payload.get("device_authorization_endpoint")
                token = payload.get("token_endpoint")
                if isinstance(device, str) and isinstance(token, str):
                    return GrokOAuthDiscovery(
                        device_authorization_endpoint=require_trusted_xai_oauth_endpoint(
                            device, field="device_authorization_endpoint"
                        ),
                        token_endpoint=require_trusted_xai_oauth_endpoint(
                            token, field="token_endpoint"
                        ),
                    )
        except (httpx.HTTPError, GrokAuthError, DeviceAuthorizationError):
            pass
        return GrokOAuthDiscovery(
            device_authorization_endpoint=require_trusted_xai_oauth_endpoint(
                self._device_code_url, field="device_authorization_endpoint"
            ),
            token_endpoint=require_trusted_xai_oauth_endpoint(
                self._token_url, field="token_endpoint"
            ),
        )

    def request_device_code(
        self,
        *,
        device_authorization_endpoint: str | None = None,
    ) -> GrokDeviceCode:
        endpoint = device_authorization_endpoint or self._device_code_url
        endpoint = require_trusted_xai_oauth_endpoint(
            endpoint, field="device_authorization_endpoint"
        )
        with httpx.Client(timeout=self._request_timeout) as client:
            response = client.post(
                endpoint,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "client_id": self._client_id,
                    "scope": self._scope,
                },
            )
        if response.status_code >= 400:
            raise DeviceAuthorizationError(
                f"xAI device-code request failed (HTTP {response.status_code}): "
                f"{response.text.strip() or 'no body'}"
            )
        try:
            return GrokDeviceCode.model_validate(self._parse_json(response))
        except ValidationError as exc:
            raise DeviceAuthorizationError(
                f"xAI returned unexpected device-code payload: {exc}"
            ) from exc

    def poll_access_token(
        self,
        device_code: GrokDeviceCode,
        *,
        token_endpoint: str | None = None,
    ) -> GrokOAuthTokens:
        endpoint = require_trusted_xai_oauth_endpoint(
            token_endpoint or self._token_url, field="token_endpoint"
        )
        interval = max(1, device_code.interval)
        deadline = time.monotonic() + device_code.expires_in

        while time.monotonic() < deadline:
            time.sleep(interval)
            with httpx.Client(timeout=self._request_timeout) as client:
                response = client.post(
                    endpoint,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": self._client_id,
                        "device_code": device_code.device_code,
                    },
                )
            if response.status_code == 200:
                try:
                    tokens = GrokOAuthTokens.model_validate(self._parse_json(response))
                except ValidationError as exc:
                    raise DeviceAuthorizationError(
                        f"xAI returned invalid access-token payload: {exc}"
                    ) from exc
                if not tokens.refresh_token:
                    raise DeviceAuthorizationError(
                        "xAI device-code token response did not include a refresh_token."
                    )
                return tokens

            payload = self._parse_json(response, allow_errors=True)
            try:
                error = GrokOAuthError.model_validate(payload)
            except ValidationError as exc:
                raise DeviceAuthorizationError(
                    f"xAI returned unexpected OAuth error payload: {exc}"
                ) from exc
            if error.error == "authorization_pending":
                continue
            if error.error == "slow_down":
                interval = min(interval + 5, 30)
                continue
            if error.error in {"expired_token", "token_expired"}:
                raise DeviceAuthorizationError(
                    "xAI device code expired before authorization completed."
                )
            if error.error == "access_denied":
                raise DeviceAuthorizationError("xAI device authorization was canceled.")
            raise DeviceAuthorizationError(self._format_error("xAI device authorization failed", error))

        raise DeviceAuthorizationError(
            "Timed out waiting for xAI device authorization."
        )

    def refresh(
        self,
        refresh_token: str,
        *,
        token_endpoint: str | None = None,
    ) -> GrokOAuthTokens:
        endpoint = require_trusted_xai_oauth_endpoint(
            token_endpoint or self._token_url, field="token_endpoint"
        )
        with httpx.Client(timeout=self._request_timeout) as client:
            response = client.post(
                endpoint,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "refresh_token": refresh_token,
                },
            )
        if response.status_code >= 400:
            detail = response.text.strip()
            if response.status_code == 403:
                raise GrokAuthError(
                    "xAI token refresh failed with HTTP 403. This OAuth account "
                    "may not be authorized for API access (tier gate). "
                    + (f"Response: {detail}" if detail else "")
                )
            raise GrokAuthError(
                f"xAI token refresh failed (HTTP {response.status_code})"
                + (f": {detail}" if detail else "")
            )
        try:
            return GrokOAuthTokens.model_validate(self._parse_json(response))
        except ValidationError as exc:
            raise GrokAuthError(f"xAI refresh returned invalid token payload: {exc}") from exc

    def build_stored_token(
        self,
        tokens: GrokOAuthTokens,
        *,
        token_endpoint: str,
        source: Literal["oauth_device_code", "oauth_refresh"],
        created_at: datetime | None = None,
        previous_refresh_token: str | None = None,
    ) -> StoredGrokToken:
        now = datetime.now(tz=UTC)
        refresh_token = tokens.refresh_token or previous_refresh_token
        if not refresh_token:
            raise GrokAuthError(
                "Cannot store Grok OAuth token without a refresh_token."
            )
        return StoredGrokToken(
            access_token=tokens.access_token,
            refresh_token=refresh_token,
            expires_at=resolve_expires_at(
                access_token=tokens.access_token,
                expires_in=tokens.expires_in,
                now=now,
            ),
            token_type=tokens.token_type or "Bearer",
            client_id=self._client_id,
            token_endpoint=require_trusted_xai_oauth_endpoint(
                token_endpoint, field="token_endpoint"
            ),
            source=source,
            created_at=created_at or now,
            updated_at=now,
        )

    @staticmethod
    def _format_error(prefix: str, error: GrokOAuthError) -> str:
        if error.error_description:
            return f"{prefix}: {error.error} ({error.error_description})"
        return f"{prefix}: {error.error}"

    @staticmethod
    def _parse_json(
        response: httpx.Response,
        *,
        allow_errors: bool = False,
    ) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeviceAuthorizationError(
                f"xAI returned non-JSON response: {response.text}"
            ) from exc
        if not isinstance(payload, dict):
            raise DeviceAuthorizationError(
                f"xAI returned unexpected response shape: {payload!r}"
            )
        if response.status_code >= 400 and not allow_errors:
            try:
                error = GrokOAuthError.model_validate(payload)
            except ValidationError:
                error = None
            if error is not None:
                raise DeviceAuthorizationError(
                    GrokOAuthClient._format_error("xAI request failed", error)
                )
            raise DeviceAuthorizationError(
                f"xAI request failed with HTTP {response.status_code}: {response.text}"
            )
        return payload

"""HTTP error classification helpers for LLM provider calls."""

from __future__ import annotations

import json
import re

import httpx

_REQUEST_FORMAT_PATTERNS = (
    re.compile(r"missing a thought_signature", re.IGNORECASE),
    re.compile(r"function call is missing", re.IGNORECASE),
    re.compile(r"\bmissing\b.*\b(function|field|parameter|argument|part)\b", re.IGNORECASE),
    re.compile(r"\binvalid\b.*\b(function|field|parameter|argument|payload|history|part)\b", re.IGNORECASE),
    re.compile(r"\bmalformed\b", re.IGNORECASE),
    re.compile(r"\bunexpected\b.*\bfield\b", re.IGNORECASE),
)


def extract_http_error_detail(text: str) -> str:
    """Extract a short textual detail from a raw HTTP response body."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    try:
        payload = json.loads(cleaned)
    except ValueError:
        return " ".join(cleaned.split())

    detail = _extract_error_detail_from_payload(payload)
    if detail:
        return detail
    return " ".join(cleaned.split())


def _extract_error_detail_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""

    # Prefer human-readable message fields before short error codes.
    for key in ("message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # OpenAI / Codex style: {"error": {"message": "...", "code": "..."}}
    err = payload.get("error")
    if isinstance(err, dict):
        message = err.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        code = err.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    elif isinstance(err, str) and err.strip():
        return err.strip()
    return ""


def format_http_error(status: int | str, body: str = "") -> str:
    """Return a compact user-facing summary from status + raw body text."""
    detail = extract_http_error_detail(body)
    if detail:
        return f"HTTP {status}: {detail}"
    return f"HTTP {status}"


def _extract_response_detail(exc: httpx.HTTPStatusError) -> str:
    """Extract a short textual detail from an HTTPStatusError response."""
    response = exc.response
    if response is None:
        return ""
    return extract_http_error_detail(response.text)


def classify_http_status_error(exc: httpx.HTTPStatusError) -> str | None:
    """Return a short category label for user-visible HTTP errors."""
    response = exc.response
    if response is None:
        return None

    status = response.status_code
    detail = _extract_response_detail(exc)

    if status == 400:
        if any(pattern.search(detail) for pattern in _REQUEST_FORMAT_PATTERNS):
            return "request-format"
        return "provider-api"

    if status in {401, 403, 404, 409, 422}:
        return "provider-api"

    return None


def format_http_status_error(exc: httpx.HTTPStatusError) -> str:
    """Return a compact user-facing HTTP error summary."""
    response = exc.response
    status = response.status_code if response is not None else "unknown"
    category = classify_http_status_error(exc)
    detail = _extract_response_detail(exc)

    prefix = f"HTTP {status}"
    if category:
        prefix += f" ({category})"
    if detail:
        return f"{prefix}: {detail}"
    return prefix

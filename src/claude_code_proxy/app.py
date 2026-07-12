"""FastAPI app for the native Claude Code proxy."""

from __future__ import annotations

import ipaddress
import secrets

from starlette.background import BackgroundTask
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from chat_agent.llm.schema import ClaudeCodeRequest

from .auth import normalize_bearer_token
from .service import (
    ClaudeCodeProxyService,
    ClaudeCodeTokenUnavailableError,
    ClaudeCodeUpstreamError,
    ClaudeCodeUpstreamTimeoutError,
)
from .settings import ClaudeCodeProxySettings


async def _close_stream(client, response) -> None:
    await response.aclose()
    await client.aclose()


def _is_loopback_client(request: Request) -> bool:
    """True when the TCP peer is a loopback address (127.0.0.0/8 or ::1).

    Uses the socket peer address only; forwarding headers are untrusted. Peers
    that cannot be parsed as an IP are treated as remote (fail closed).
    """

    if request.client is None:
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    # A dual-stack bind reports local IPv4 peers as ::ffff:127.0.0.1.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _reject_unauthorized(request: Request, api_key: str | None) -> JSONResponse | None:
    """Gate non-loopback clients behind the inbound API key.

    Loopback requests always pass. Without a configured key, remote requests
    are always rejected so binding a public host can never silently expose
    upstream quota.
    """

    if _is_loopback_client(request):
        return None
    if not api_key:
        return JSONResponse(
            {
                "error": (
                    "This proxy only accepts localhost requests. Set "
                    "CLAUDE_CODE_PROXY_API_KEY (or serve --api-key) to allow "
                    "remote clients."
                )
            },
            status_code=401,
        )
    provided = request.headers.get("x-api-key") or normalize_bearer_token(
        request.headers.get("authorization", "")
    )
    if provided and secrets.compare_digest(provided, api_key):
        return None
    return JSONResponse(
        {
            "error": (
                "Invalid or missing API key. Provide it via x-api-key or "
                "Authorization: Bearer."
            )
        },
        status_code=401,
    )


def create_app(settings: ClaudeCodeProxySettings) -> FastAPI:
    app = FastAPI(title="chat-agent-claude-code-proxy", docs_url=None, redoc_url=None)
    service = ClaudeCodeProxyService(settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/usage")
    async def usage(raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        return await service.usage_snapshot()

    @app.get("/v1/models")
    async def models(raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        try:
            body, media_type = await service.forward_models(raw_request.url.query)
        except ClaudeCodeUpstreamError as exc:
            return Response(
                content=exc.body,
                status_code=exc.status_code,
                media_type=exc.media_type,
            )
        except ClaudeCodeTokenUnavailableError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        return Response(content=body, media_type=media_type)

    @app.post("/v1/messages")
    async def messages(request: ClaudeCodeRequest, raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        try:
            if request.stream:
                client, upstream = await service.open_stream(request)
                return StreamingResponse(
                    upstream.aiter_raw(),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "text/event-stream"),
                    background=BackgroundTask(_close_stream, client, upstream),
                )

            body, media_type = await service.forward_json(request)
        except ClaudeCodeUpstreamError as exc:
            return Response(
                content=exc.body,
                status_code=exc.status_code,
                media_type=exc.media_type,
            )
        except ClaudeCodeTokenUnavailableError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except ClaudeCodeUpstreamTimeoutError as exc:
            return JSONResponse({"error": str(exc)}, status_code=504)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return Response(content=body, media_type=media_type)

    return app

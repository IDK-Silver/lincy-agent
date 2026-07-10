"""FastAPI app for the native SuperGrok OAuth proxy."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .auth import GrokAuthError
from .service import (
    GrokProxyService,
    GrokTokenUnavailableError,
    GrokUpstreamError,
)
from .settings import GrokProxySettings


async def _close_stream(client, response) -> None:
    await response.aclose()
    await client.aclose()


def _wants_stream(body: Any) -> bool:
    return isinstance(body, dict) and body.get("stream") is True


def _cache_forward_headers(request: Request) -> dict[str, str]:
    """Extract client headers that must be forwarded for xAI prompt-cache routing."""

    value = request.headers.get("x-grok-conv-id")
    if value and value.strip():
        return {"x-grok-conv-id": value.strip()}
    return {}


def create_app(settings: GrokProxySettings) -> FastAPI:
    app = FastAPI(title="chat-agent-grok-proxy", docs_url=None, redoc_url=None)
    service = GrokProxyService(settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _handle_post(path: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be JSON"}, status_code=400)

        extra_headers = _cache_forward_headers(request)
        # Responses API sticky routing uses body prompt_cache_key (equivalent role
        # to x-grok-conv-id on Chat Completions). Inject when client only sent header.
        if (
            path.rstrip("/").endswith("/responses")
            and isinstance(body, dict)
            and not body.get("prompt_cache_key")
            and "x-grok-conv-id" in extra_headers
        ):
            body = {**body, "prompt_cache_key": extra_headers["x-grok-conv-id"]}

        try:
            if _wants_stream(body):
                client, upstream = await service.open_stream(
                    "POST",
                    path,
                    json_body=body,
                    extra_headers=extra_headers,
                )
                return StreamingResponse(
                    upstream.aiter_raw(),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get(
                        "content-type", "text/event-stream"
                    ),
                    background=BackgroundTask(_close_stream, client, upstream),
                )

            content, media_type, status = await service.forward_json(
                "POST",
                path,
                json_body=body,
                extra_headers=extra_headers,
            )
        except GrokUpstreamError as exc:
            return Response(
                content=exc.body,
                status_code=exc.status_code,
                media_type=exc.media_type,
            )
        except GrokTokenUnavailableError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except GrokAuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return Response(content=content, status_code=status, media_type=media_type)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _handle_post("/v1/chat/completions", request)

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await _handle_post("/v1/responses", request)

    @app.get("/v1/models")
    async def models():
        try:
            content, media_type, status = await service.forward_json("GET", "/v1/models")
        except GrokUpstreamError as exc:
            return Response(
                content=exc.body,
                status_code=exc.status_code,
                media_type=exc.media_type,
            )
        except GrokTokenUnavailableError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except GrokAuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        return Response(content=content, status_code=status, media_type=media_type)

    return app

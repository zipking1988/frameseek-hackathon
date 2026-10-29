"""Stateless Cloud Run ingress for a single durable workspace backend.

No storage, credentials for Gemini, model calls, cloud SDK or resource creation.
The backend must be explicitly configured; there is no fixture fallback.
"""

import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
TOKEN_HEADER = "x-fs-gateway-token"
MAX_BODY_BYTES = 30 * 1024**2  # Uvicorn is HTTP/1; stay below Cloud Run's 32 MiB limit.


class GatewayBodyLimit(ValueError):
    pass


async def bounded_body(request):
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise GatewayBodyLimit
        yield chunk


def https_origin(value):
    parsed = urlparse(value or "")
    try:
        port = parsed.port
    except ValueError:
        raise RuntimeError("An explicit HTTPS origin is required") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RuntimeError("An explicit HTTPS origin is required")
    return f"https://{parsed.netloc}"


def gateway_token(value):
    if not value or not 32 <= len(value) <= 256 or not value.isascii() or not value.isprintable():
        raise RuntimeError("Gateway requires a private shared token of 32–256 ASCII characters")
    return value


def filtered_headers(headers, *, request=False):
    connection = {h.strip().lower() for h in headers.get("connection", "").split(",")}
    excluded = HOP_HEADERS | connection | {TOKEN_HEADER}
    if request:
        # Forwarding metadata is rebuilt from reviewed configuration, not browser headers.
        excluded |= {
            "host",
            "forwarded",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-forwarded-for",
        }
    return [
        (key, value) for key, value in headers.raw if key.decode("latin-1").lower() not in excluded
    ]


def create_gateway(*, transport=None):
    public = https_origin(os.environ.get("FS_PUBLIC_ORIGIN"))
    backend = https_origin(os.environ.get("FS_BACKEND_ORIGIN"))
    if public == backend:
        raise RuntimeError("Gateway backend must differ from the public origin")
    token = gateway_token(os.environ.get("FS_GATEWAY_TOKEN"))

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(connect=10, read=300, write=180, pool=10),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(public).hostname])

    @app.get("/gateway/health")
    def health():
        return {
            "component": "stateless_gateway",
            "backend_verified": False,
            "model_requests_sent": 0,
        }

    @app.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def proxy(request: Request, path: str):
        try:
            size = int(request.headers.get("content-length", "0"))
            if size < 0:
                raise ValueError
        except ValueError:
            return JSONResponse({"detail": "invalid_upload_length"}, status_code=400)
        if size > MAX_BODY_BYTES:
            return JSONResponse({"detail": "gateway_upload_limit_30_mib"}, status_code=413)
        raw_path = request.scope["raw_path"]
        if request.scope["query_string"]:
            raw_path += b"?" + request.scope["query_string"]
        target = httpx.URL(backend).copy_with(raw_path=raw_path)
        headers = filtered_headers(request.headers, request=True)
        headers += [
            (b"host", urlparse(public).netloc.encode()),
            (TOKEN_HEADER.encode(), token.encode()),
            (b"x-forwarded-proto", b"https"),
        ]
        # GET/HEAD requests have no streaming body; preserve range and cache headers.
        body = bounded_body(request) if request.method not in {"GET", "HEAD"} else None
        # Build directly: a pooled client's cookie jar must never supply another
        # browser's session when this request has no Cookie header.
        outgoing = httpx.Request(request.method, target, headers=headers, content=body)
        try:
            response = await app.state.client.send(outgoing, stream=True)
        except GatewayBodyLimit:
            return JSONResponse(
                {"detail": "gateway_upload_limit_30_mib"},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        except httpx.HTTPError:
            return JSONResponse(
                {"detail": "workspace_backend_unavailable"},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )

        async def chunks():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        result = StreamingResponse(
            chunks(), status_code=response.status_code, background=BackgroundTask(response.aclose)
        )
        # Keep duplicate Set-Cookie headers and the original encoding/content length.
        result.raw_headers = filtered_headers(response.headers)
        return result

    return app

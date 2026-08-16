"""aiohttp application factory for the bridge."""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from aiohttp import hdrs, web
from aiohttp.http import HttpVersion11

from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.errors import openai_error_response
from codex_openai_bridge.security import bearer_is_authorized, load_bridge_token


class CredentialProvider(Protocol):
    """Credential capability required by readiness and upstream calls."""

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential: ...


_TOKEN_KEY = web.AppKey("bridge_token", str)
_CREDENTIAL_PROVIDER_KEY = web.AppKey("credential_provider", CredentialProvider)
_CANONICAL_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_READINESS_EXPIRY_SKEW_SECONDS = 60
_REQUEST_ID_BYTES = 16
_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_KEY = web.RequestKey("request_id", str)


def _assign_request_id(request: web.Request) -> str:
    request_id = secrets.token_hex(_REQUEST_ID_BYTES)
    request[_REQUEST_ID_KEY] = request_id
    return request_id


@web.middleware
async def _request_id_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    request_id = _assign_request_id(request)
    response = await handler(request)
    response.headers[_REQUEST_ID_HEADER] = request_id
    return response


def _request_is_authorized(request: web.Request) -> bool:
    values = request.headers.getall("Authorization", [])
    return len(values) == 1 and bearer_is_authorized(values[0], request.app[_TOKEN_KEY])


async def _protected_expect_handler(request: web.Request) -> web.StreamResponse | None:
    if not _request_is_authorized(request):
        response = openai_error_response(
            status=401,
            message="Invalid authentication credentials",
            error_type="invalid_request_error",
            code="invalid_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )
        response.headers[_REQUEST_ID_HEADER] = _assign_request_id(request)
        return response
    expect_values = request.headers.getall(hdrs.EXPECT, [])
    if request.version == HttpVersion11:
        if len(expect_values) == 1 and expect_values[0].lower() == "100-continue":
            await request.writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            request.writer.output_size = 0
        else:
            raise web.HTTPExpectationFailed(text="Unsupported expectation")
    return None


@web.middleware
async def _client_auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    if request.path.startswith("/v1/") and not _request_is_authorized(request):
        return openai_error_response(
            status=401,
            message="Invalid authentication credentials",
            error_type="invalid_request_error",
            code="invalid_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await handler(request)


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _credential_is_ready(value: object, *, now: float) -> bool:
    return (
        type(value) is Credential
        and type(value.access_token) is str
        and bool(value.access_token)
        and type(value.base_url) is str
        and value.base_url == _CANONICAL_CODEX_BASE_URL
        and (value.account_id is None or (type(value.account_id) is str and bool(value.account_id)))
        and type(value.expires_at) is int
        and value.expires_at > now + _READINESS_EXPIRY_SKEW_SECONDS
    )


async def _readyz(request: web.Request) -> web.Response:
    try:
        credential = await request.app[_CREDENTIAL_PROVIDER_KEY].get_credentials()
    except Exception:
        return web.json_response({"status": "unavailable"}, status=503)
    if _credential_is_ready(credential, now=time.time()):
        return web.json_response({"status": "ready"})
    return web.json_response({"status": "unavailable"}, status=503)


async def _not_implemented(_request: web.Request) -> web.Response:
    return openai_error_response(
        status=501,
        message="Endpoint is not implemented",
        error_type="invalid_request_error",
        code="unsupported_endpoint",
    )


def create_app(settings: Settings, credential_provider: CredentialProvider) -> web.Application:
    """Create the loopback bridge application with startup-loaded client auth."""
    app = web.Application(
        middlewares=[_request_id_middleware, _client_auth_middleware],
        client_max_size=settings.max_request_body_bytes,
    )
    app[_TOKEN_KEY] = load_bridge_token(settings.client_token_file)
    app[_CREDENTIAL_PROVIDER_KEY] = credential_provider
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/readyz", _readyz)
    app.router.add_post(
        "/v1/chat/completions",
        _not_implemented,
        expect_handler=_protected_expect_handler,
    )
    return app

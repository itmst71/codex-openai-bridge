"""aiohttp application factory for the bridge."""

from __future__ import annotations

import asyncio
import secrets
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, cast

from aiohttp import hdrs, web
from aiohttp.http import HttpVersion11

from codex_openai_bridge.admission import (
    AdmissionController,
    AdmissionLease,
    AdmissionQueueTimeout,
    AdmissionShuttingDown,
)
from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.errors import openai_error_response
from codex_openai_bridge.json_boundary import (
    JsonBodyTooLarge,
    JsonBoundaryError,
    read_json_request,
    validate_json_request_headers,
)
from codex_openai_bridge.responses import (
    ResponsesRequestError,
    parse_responses_request,
    responses_request_to_upstream,
    responses_to_public,
)
from codex_openai_bridge.security import bearer_is_authorized, load_bridge_token
from codex_openai_bridge.translation import (
    UpstreamResponseError,
    chat_request_to_responses,
    responses_to_chat_completion,
    translate_responses_sse,
)
from codex_openai_bridge.upstream import (
    HttpxResponsesUpstream,
    ResponsesByteStream,
    ResponsesUpstream,
    StreamingResponsesUpstream,
    UpstreamError,
    UpstreamErrorKind,
)
from codex_openai_bridge.wire import ChatRequestError, parse_chat_completion_request


class CredentialProvider(Protocol):
    """Credential capability required by readiness and upstream calls."""

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential: ...


_TOKEN_KEY = web.AppKey("bridge_token", str)
_CREDENTIAL_PROVIDER_KEY = web.AppKey("credential_provider", CredentialProvider)
_SETTINGS_KEY = web.AppKey("settings", Settings)
_UPSTREAM_KEY = web.AppKey("upstream", object)
_ADMISSION_KEY = web.AppKey("admission", AdmissionController)
_CANONICAL_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_READINESS_EXPIRY_SKEW_SECONDS = 60
_REQUEST_ID_BYTES = 16
_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_KEY = web.RequestKey("request_id", str)
_ADMISSION_LEASE_KEY = web.RequestKey("admission_lease", AdmissionLease)
_GENERATION_PATHS = frozenset({"/v1/chat/completions", "/v1/responses"})


def _assign_request_id(request: web.Request) -> str:
    request_id = secrets.token_hex(_REQUEST_ID_BYTES)
    request[_REQUEST_ID_KEY] = request_id
    return request_id


def _response_request_id(request: web.Request) -> str:
    try:
        candidate = request.get(_REQUEST_ID_KEY)
    except (AttributeError, TypeError):
        candidate = None
    if (
        type(candidate) is str
        and len(candidate) == _REQUEST_ID_BYTES * 2
        and all(character in "0123456789abcdef" for character in candidate)
    ):
        return candidate
    return secrets.token_hex(_REQUEST_ID_BYTES)


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


def _json_boundary_response(error: JsonBoundaryError) -> web.Response:
    if isinstance(error, JsonBodyTooLarge):
        return openai_error_response(
            status=413,
            message="Request body is too large",
            error_type="invalid_request_error",
            code="request_too_large",
        )
    return openai_error_response(
        status=400,
        message="Request JSON is invalid",
        error_type="invalid_request_error",
        code="invalid_json",
    )


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
    try:
        validate_json_request_headers(
            request,
            max_body_bytes=request.app[_SETTINGS_KEY].max_request_body_bytes,
        )
    except JsonBoundaryError as error:
        response = _json_boundary_response(error)
        response.headers[_REQUEST_ID_HEADER] = _assign_request_id(request)
        return response
    if request.version != HttpVersion11:
        return None
    expect_values = request.headers.getall(hdrs.EXPECT, [])
    if len(expect_values) != 1 or expect_values[0].lower() != "100-continue":
        raise web.HTTPExpectationFailed(text="Unsupported expectation")
    try:
        lease = await request.app[_ADMISSION_KEY].acquire()
    except AdmissionQueueTimeout:
        response = _queue_timeout_response()
        response.headers[_REQUEST_ID_HEADER] = _assign_request_id(request)
        return response
    except AdmissionShuttingDown:
        response = _shutdown_response()
        response.headers[_REQUEST_ID_HEADER] = _assign_request_id(request)
        return response
    request[_ADMISSION_LEASE_KEY] = lease
    try:
        await request.writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        request.writer.output_size = 0
    except BaseException:
        lease.release()
        del request[_ADMISSION_LEASE_KEY]
        raise
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


def _queue_timeout_response() -> web.Response:
    return openai_error_response(
        status=429,
        message="Too many requests",
        error_type="rate_limit_error",
        code="bridge_queue_timeout",
    )


def _shutdown_response() -> web.Response:
    return openai_error_response(
        status=503,
        message="Service unavailable",
        error_type="server_error",
        code="bridge_shutting_down",
    )


def _is_generation_request(request: web.Request) -> bool:
    return request.method == hdrs.METH_POST and request.path in _GENERATION_PATHS


@web.middleware
async def _admission_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    if not _is_generation_request(request):
        return await handler(request)
    lease = request.get(_ADMISSION_LEASE_KEY)
    if lease is None:
        try:
            lease = await request.app[_ADMISSION_KEY].acquire()
        except AdmissionQueueTimeout:
            return _queue_timeout_response()
        except AdmissionShuttingDown:
            return _shutdown_response()
        request[_ADMISSION_LEASE_KEY] = lease
    try:
        return await handler(request)
    finally:
        lease.release()
        if request.get(_ADMISSION_LEASE_KEY) is lease:
            del request[_ADMISSION_LEASE_KEY]


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


def _invalid_request_response() -> web.Response:
    return openai_error_response(
        status=400,
        message="Request is invalid",
        error_type="invalid_request_error",
        code="invalid_request",
    )


def _service_error_response() -> web.Response:
    return openai_error_response(
        status=502,
        message="Upstream service unavailable",
        error_type="server_error",
        code="upstream_error",
    )


def _credentials_error_response() -> web.Response:
    return openai_error_response(
        status=503,
        message="Upstream credentials unavailable",
        error_type="server_error",
        code="credentials_unavailable",
    )


def _upstream_error_response(error: UpstreamError) -> web.Response:
    if error.kind is UpstreamErrorKind.CREDENTIALS:
        return _credentials_error_response()
    if error.kind is UpstreamErrorKind.TIMEOUT:
        return openai_error_response(
            status=504,
            message="Upstream request timed out",
            error_type="server_error",
            code="upstream_timeout",
        )
    if error.kind is UpstreamErrorKind.RATE_LIMIT:
        headers = {"Retry-After": error.retry_after} if error.retry_after is not None else None
        return openai_error_response(
            status=429,
            message="Upstream rate limit exceeded",
            error_type="rate_limit_error",
            code="rate_limit_exceeded",
            headers=headers,
        )
    return _service_error_response()


_STREAM_ERROR_FRAME = (
    b'data: {"error":{"message":"Upstream stream failed","type":"server_error",'
    b'"param":null,"code":"upstream_stream_error"}}\n\n'
)


async def _with_deadline(awaitable: Awaitable[object], *, deadline: float) -> object:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(remaining):
        result = await awaitable
    if time.monotonic() > deadline:
        raise TimeoutError
    return result


async def _close_stream_preserving_primary(stream: ResponsesByteStream) -> None:
    primary_error = sys.exception()
    try:
        await stream.aclose()
    except BaseException:
        if primary_error is None:
            raise


async def _close_stream_after_prepare(stream: ResponsesByteStream) -> None:
    primary_error = sys.exception()
    try:
        await stream.aclose()
    except asyncio.CancelledError:
        if primary_error is None:
            raise
    except Exception:
        pass
    except BaseException:
        if primary_error is None:
            raise


async def _write_stream_frame(
    response: web.StreamResponse,
    frame: bytes,
    *,
    deadline: float,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(remaining):
        await response.write(frame)
    if time.monotonic() > deadline:
        raise TimeoutError


async def _stream_chat_completion(
    request: web.Request,
    *,
    credential: Credential,
    payload: dict[str, object],
    include_usage: bool,
    deadline: float,
    request_id: str | None = None,
) -> web.StreamResponse:
    settings = request.app[_SETTINGS_KEY]
    if request_id is not None and (
        type(request_id) is not str
        or len(request_id) != _REQUEST_ID_BYTES * 2
        or any(character not in "0123456789abcdef" for character in request_id)
    ):
        raise UpstreamResponseError("invalid upstream response")
    terminal_reserve = min(0.25, settings.total_request_deadline_seconds / 10)
    body_deadline = deadline - terminal_reserve
    if time.monotonic() >= body_deadline:
        raise UpstreamError(UpstreamErrorKind.TIMEOUT)
    upstream = cast(StreamingResponsesUpstream, request.app[_UPSTREAM_KEY])
    stream: ResponsesByteStream | None = None
    translated: AsyncIterator[bytes] | None = None
    prefetched = False
    try:
        stream_result = await _with_deadline(
            upstream.create_stream(credential, payload),
            deadline=body_deadline,
        )
        stream = cast(ResponsesByteStream, stream_result)
        translated = translate_responses_sse(
            stream.__aiter__(),
            public_model=settings.public_model,
            include_usage=include_usage,
            max_sse_event_bytes=settings.max_sse_event_bytes,
            max_stream_bytes=settings.max_stream_bytes,
            max_json_depth=settings.max_json_depth,
            max_json_nodes=settings.max_json_nodes,
            max_string_bytes=settings.max_string_bytes,
        )
        first_result = await _with_deadline(anext(translated), deadline=body_deadline)
        first_frame = cast(bytes, first_result)
        prefetched = True
    except UpstreamError:
        raise
    except UpstreamResponseError:
        raise
    except TimeoutError:
        raise UpstreamError(UpstreamErrorKind.TIMEOUT) from None
    except asyncio.CancelledError:
        raise
    except Exception:
        raise UpstreamError(UpstreamErrorKind.SERVICE) from None
    finally:
        if stream is not None and not prefetched:
            await _close_stream_preserving_primary(stream)

    response_headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
    }
    if request_id is not None:
        response_headers[_REQUEST_ID_HEADER] = request_id
    response = web.StreamResponse(status=200, headers=response_headers)
    response_prepared = False
    try:
        await _with_deadline(response.prepare(request), deadline=body_deadline)
        response_prepared = True
        try:
            await _write_stream_frame(response, first_frame, deadline=body_deadline)
            assert translated is not None
            while True:
                next_result = await _with_deadline(anext(translated), deadline=body_deadline)
                frame = cast(bytes, next_result)
                await _write_stream_frame(response, frame, deadline=body_deadline)
        except StopAsyncIteration:
            pass
        except (UpstreamError, UpstreamResponseError, TimeoutError):
            await _write_stream_frame(response, _STREAM_ERROR_FRAME, deadline=deadline)
    finally:
        assert stream is not None
        if response_prepared:
            await _close_stream_after_prepare(stream)
        else:
            await _close_stream_preserving_primary(stream)
    return response


async def _chat_completions(request: web.Request) -> web.Response:
    deadline = time.monotonic() + request.app[_SETTINGS_KEY].total_request_deadline_seconds
    settings = request.app[_SETTINGS_KEY]
    try:
        document_result = await _with_deadline(
            read_json_request(
                request,
                max_body_bytes=settings.max_request_body_bytes,
                max_depth=settings.max_json_depth,
                max_nodes=settings.max_json_nodes,
                max_string_bytes=settings.max_string_bytes,
            ),
            deadline=deadline,
        )
        document = document_result
    except TimeoutError:
        return _upstream_error_response(UpstreamError(UpstreamErrorKind.TIMEOUT))
    except JsonBoundaryError as error:
        return _json_boundary_response(error)
    try:
        chat_request = parse_chat_completion_request(
            document,
            public_model=settings.public_model,
            max_messages=settings.max_messages,
            max_tools=settings.max_tools,
            max_json_depth=settings.max_json_depth,
            max_json_nodes=settings.max_json_nodes,
            max_string_bytes=settings.max_string_bytes,
            binding_key=request.app[_TOKEN_KEY],
        )
        payload = chat_request_to_responses(chat_request, upstream_model=settings.upstream_model)
    except ChatRequestError:
        return _invalid_request_response()

    try:
        credential_result = await _with_deadline(
            request.app[_CREDENTIAL_PROVIDER_KEY].get_credentials(),
            deadline=deadline,
        )
        credential = cast(Credential, credential_result)
    except TimeoutError:
        return _upstream_error_response(UpstreamError(UpstreamErrorKind.TIMEOUT))
    except Exception:
        return _credentials_error_response()
    if chat_request.stream:
        try:
            return cast(
                web.Response,
                await _stream_chat_completion(
                    request,
                    credential=credential,
                    payload=payload,
                    include_usage=chat_request.include_usage,
                    deadline=deadline,
                    request_id=_response_request_id(request),
                ),
            )
        except UpstreamError as error:
            return _upstream_error_response(error)
        except UpstreamResponseError:
            return _service_error_response()
    try:
        upstream = cast(ResponsesUpstream, request.app[_UPSTREAM_KEY])
        upstream_response = await _with_deadline(
            upstream.create_response(credential, payload), deadline=deadline
        )
        completion = responses_to_chat_completion(
            upstream_response,
            public_model=settings.public_model,
            binding_key=request.app[_TOKEN_KEY],
            max_json_depth=settings.max_json_depth,
            max_json_nodes=settings.max_json_nodes,
            max_string_bytes=settings.max_string_bytes,
        )
    except UpstreamError as error:
        return _upstream_error_response(error)
    except UpstreamResponseError:
        return _service_error_response()
    except Exception:
        return _service_error_response()
    return web.json_response(completion)


async def _responses(request: web.Request) -> web.Response:
    deadline = time.monotonic() + request.app[_SETTINGS_KEY].total_request_deadline_seconds
    settings = request.app[_SETTINGS_KEY]
    try:
        document = await _with_deadline(
            read_json_request(
                request,
                max_body_bytes=settings.max_request_body_bytes,
                max_depth=settings.max_json_depth,
                max_nodes=settings.max_json_nodes,
                max_string_bytes=settings.max_string_bytes,
            ),
            deadline=deadline,
        )
    except TimeoutError:
        return _upstream_error_response(UpstreamError(UpstreamErrorKind.TIMEOUT))
    except JsonBoundaryError as error:
        return _json_boundary_response(error)
    try:
        responses_request = parse_responses_request(
            document,
            public_model=settings.public_model,
            max_items=settings.max_messages,
            max_tools=settings.max_tools,
            max_json_depth=settings.max_json_depth,
            max_json_nodes=settings.max_json_nodes,
            max_string_bytes=settings.max_string_bytes,
        )
        payload = responses_request_to_upstream(
            responses_request,
            upstream_model=settings.upstream_model,
        )
    except ResponsesRequestError:
        return _invalid_request_response()
    if time.monotonic() > deadline:
        return _upstream_error_response(UpstreamError(UpstreamErrorKind.TIMEOUT))

    try:
        credential_result = await _with_deadline(
            request.app[_CREDENTIAL_PROVIDER_KEY].get_credentials(),
            deadline=deadline,
        )
        credential = cast(Credential, credential_result)
    except TimeoutError:
        return _upstream_error_response(UpstreamError(UpstreamErrorKind.TIMEOUT))
    except Exception:
        return _credentials_error_response()
    try:
        upstream = cast(ResponsesUpstream, request.app[_UPSTREAM_KEY])
        upstream_response = await _with_deadline(
            upstream.create_response(credential, payload),
            deadline=deadline,
        )
        public_response = responses_to_public(
            upstream_response,
            request=responses_request,
            public_model=settings.public_model,
            max_items=settings.max_messages,
            max_tools=settings.max_tools,
            max_json_depth=settings.max_json_depth,
            max_json_nodes=settings.max_json_nodes,
            max_string_bytes=settings.max_string_bytes,
        )
        if time.monotonic() > deadline:
            raise UpstreamError(UpstreamErrorKind.TIMEOUT)
    except UpstreamError as error:
        return _upstream_error_response(error)
    except UpstreamResponseError:
        return _service_error_response()
    except Exception:
        return _service_error_response()
    return web.json_response(public_response)


def create_app(
    settings: Settings,
    credential_provider: CredentialProvider,
    *,
    upstream: ResponsesUpstream | StreamingResponsesUpstream | None = None,
) -> web.Application:
    """Create the loopback bridge application with startup-loaded client auth."""
    app = web.Application(
        middlewares=[_request_id_middleware, _client_auth_middleware, _admission_middleware],
        client_max_size=settings.max_request_body_bytes,
    )
    app[_TOKEN_KEY] = load_bridge_token(settings.client_token_file)
    app[_CREDENTIAL_PROVIDER_KEY] = credential_provider
    app[_SETTINGS_KEY] = settings
    app[_ADMISSION_KEY] = AdmissionController(
        max_in_flight=settings.max_in_flight,
        queue_wait_seconds=settings.queue_wait_seconds,
    )

    async def shutdown_admission(application: web.Application) -> None:
        await application[_ADMISSION_KEY].shutdown()

    app.on_shutdown.append(shutdown_admission)
    app.on_cleanup.append(shutdown_admission)
    if upstream is None:

        async def refresh_credentials() -> Credential:
            return await credential_provider.get_credentials(force_refresh=True)

        owned_upstream = HttpxResponsesUpstream(
            settings,
            credential_refresher=refresh_credentials,
        )
        app[_UPSTREAM_KEY] = owned_upstream

        async def close_owned_upstream(_app: web.Application) -> None:
            await owned_upstream.aclose()

        app.on_cleanup.append(close_owned_upstream)
    else:
        app[_UPSTREAM_KEY] = upstream
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/readyz", _readyz)
    app.router.add_post(
        "/v1/chat/completions",
        _chat_completions,
        expect_handler=_protected_expect_handler,
    )
    app.router.add_post(
        "/v1/responses",
        _responses,
        expect_handler=_protected_expect_handler,
    )
    return app

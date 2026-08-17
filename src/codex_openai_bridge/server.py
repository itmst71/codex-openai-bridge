"""Secret-negative aiohttp protocol error handling."""

from __future__ import annotations

import secrets
from types import MethodType
from typing import Any, cast

from aiohttp import hdrs, web
from aiohttp.web_protocol import RequestHandler
from aiohttp.web_request import BaseRequest
from aiohttp.web_server import Server

from codex_openai_bridge.logging import emit_request_log, endpoint_class

_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_BYTES = 16


def _emit_protocol_log_safely(
    *,
    request_id: str,
    endpoint: str,
    status: int,
    request_bytes: int | None,
    response_bytes: int,
) -> None:
    try:
        emit_request_log(
            request_id=request_id,
            endpoint=endpoint,
            status=status,
            duration_ms=0,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
        )
    except BaseException:
        return


def _sanitized_protocol_response(
    *,
    status: int,
    body: str,
    endpoint: str,
    request_bytes: int | None = None,
) -> web.Response:
    request_id = secrets.token_hex(_REQUEST_ID_BYTES)
    response = web.Response(
        status=status,
        text=body,
        content_type="text/plain",
        headers={_REQUEST_ID_HEADER: request_id},
    )
    response.force_close()
    _emit_protocol_log_safely(
        request_id=request_id,
        endpoint=endpoint,
        status=status,
        request_bytes=request_bytes,
        response_bytes=len(body.encode("utf-8")),
    )
    return response


class _SanitizedRequestHandler(RequestHandler):
    def handle_error(
        self,
        request: BaseRequest,
        status: int = 500,
        exc: BaseException | None = None,
        message: str | None = None,
    ) -> web.StreamResponse:
        del exc, message
        if request.writer.output_size > 0:
            raise ConnectionError("Response is already sent")
        safe_status = status if type(status) is int and 400 <= status <= 599 else 500
        return _sanitized_protocol_response(
            status=safe_status,
            body=("Invalid HTTP request" if safe_status < 500 else "Internal server error"),
            endpoint="other",
        )


class _SanitizedServer(Server):
    def __call__(self) -> RequestHandler:
        server = cast(Any, self)
        return _SanitizedRequestHandler(
            self,
            loop=server._loop,
            **dict(server._kwargs),
        )


def install_sanitized_protocol(app: web.Application) -> None:
    """Install a fail-closed protocol factory on one application instance."""

    app_any = cast(Any, app)
    original_make_handler = app_any._make_handler

    def make_handler(_app: web.Application, **kwargs: Any) -> Server:
        base = original_make_handler(**kwargs)
        base_any = cast(Any, base)

        async def reject_unsupported_expect(request: BaseRequest) -> web.StreamResponse:
            expect_values = request.headers.getall(hdrs.EXPECT, [])
            if expect_values and (
                len(expect_values) != 1 or expect_values[0].lower() != "100-continue"
            ):
                content_length = request.content_length
                return _sanitized_protocol_response(
                    status=417,
                    body="Unsupported expectation",
                    endpoint=endpoint_class(request.method, request.path),
                    request_bytes=(
                        content_length
                        if type(content_length) is int and content_length >= 0
                        else None
                    ),
                )
            return cast(web.StreamResponse, await base.request_handler(request))

        return _SanitizedServer(
            reject_unsupported_expect,
            request_factory=base.request_factory,
            handler_cancellation=base.handler_cancellation,
            loop=base_any._loop,
            **dict(base_any._kwargs),
        )

    app_any._make_handler = MethodType(make_handler, app)

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import codex_openai_bridge.app as app_module
import codex_openai_bridge.logging as bridge_logging
from codex_openai_bridge.admission import AdmissionQueueTimeout, AdmissionShuttingDown
from codex_openai_bridge.app import create_app
from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.upstream import UpstreamError
from codex_openai_bridge.wire import ToolCall, create_reasoning_binding_id

TOKEN = "a" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LOGGER_NAME = "codex_openai_bridge.requests"
PROMPT_CANARY = "PROMPT_CANARY_7d35"
TOOL_CANARY = "TOOL_ARGUMENT_CANARY_1ac9"
TOKEN_CANARY = "UPSTREAM_TOKEN_CANARY_42ef"
ACCOUNT_CANARY = "ACCOUNT_CANARY_b677"
QUERY_CANARY = "UPSTREAM_QUERY_CANARY_d893"
REASONING_CANARY = base64.b64encode(b"REASONING_CANARY_92cd").decode("ascii")
CANARIES = (
    TOKEN,
    PROMPT_CANARY,
    TOOL_CANARY,
    TOKEN_CANARY,
    ACCOUNT_CANARY,
    QUERY_CANARY,
    REASONING_CANARY,
    "https://chatgpt.com/backend-api/codex",
)
_ALLOWED_LOG_FIELDS = {
    "request_id",
    "endpoint",
    "status",
    "duration_ms",
    "request_bytes",
    "response_bytes",
    "code",
}


class StaticCredentialManager:
    def __init__(self) -> None:
        self.credential = Credential(
            access_token=TOKEN_CANARY,
            base_url=f"https://chatgpt.com/backend-api/codex?marker={QUERY_CANARY}",
            account_id=ACCOUNT_CANARY,
            expires_at=4_102_444_800,
        )

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        del force_refresh
        return self.credential


class CanaryFailingUpstream:
    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        del credential, payload
        raise RuntimeError("|".join(CANARIES))


def _settings(tmp_path: Path) -> Settings:
    token_file = tmp_path / "client-token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    continuation_key_file = tmp_path / "continuation-key"
    continuation_key_file.write_text("b" * 43 + "\n", encoding="ascii")
    continuation_key_file.chmod(0o600)
    return replace(
        Settings.from_env(),
        client_token_file=token_file,
        continuation_key_file=continuation_key_file,
    )


def _request_document() -> dict[str, object]:
    call = ToolCall(
        call_id="call_logging",
        name="lookup",
        arguments=json.dumps({"secret": TOOL_CANARY}),
    )
    detail = {
        "type": "reasoning.encrypted",
        "data": REASONING_CANARY,
        "format": "openai-responses-v1",
        "id": create_reasoning_binding_id(
            binding_key="b" * 43,
            content=None,
            tool_calls=(call,),
            index=0,
            data=REASONING_CANARY,
        ),
        "index": 0,
    }
    return {
        "model": "codex",
        "messages": [
            {"role": "user", "content": PROMPT_CANARY},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                ],
                "reasoning_details": [detail],
            },
            {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": "bounded result",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                    },
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_request_observability_is_closed_and_all_sensitive_surfaces_are_absent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging.getLogger("aiohttp.access"), "disabled", True)
    app = create_app(
        _settings(tmp_path),
        StaticCredentialManager(),
        upstream=CanaryFailingUpstream(),
    )

    async with TestClient(TestServer(app, access_log=None)) as client:
        response = await client.post(
            "/v1/chat/completions?client_query=QUERY_INPUT_CANARY",
            json=_request_document(),
            headers=AUTH,
        )
        body = await response.json()

    assert response.status == 502
    assert body == {
        "error": {
            "message": "Upstream service unavailable",
            "type": "server_error",
            "param": None,
            "code": "upstream_error",
        }
    }
    captured = caplog.text + repr(body)
    for canary in (*CANARIES, "QUERY_INPUT_CANARY"):
        assert canary not in captured

    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert len(records) == 1
    event = json.loads(records[0].getMessage())
    assert set(event) <= _ALLOWED_LOG_FIELDS
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["endpoint"] == "chat_completions"
    assert event["status"] == 502
    assert event["code"] == "upstream_error"
    assert type(event["duration_ms"]) is int and event["duration_ms"] >= 0
    assert type(event["request_bytes"]) is int and event["request_bytes"] > 0
    assert type(event["response_bytes"]) is int and event["response_bytes"] > 0


@pytest.mark.asyncio
async def test_unexpected_middleware_exception_is_sanitized_without_exception_logging(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exception_canary = "AUTH_EXCEPTION_CANARY_e41b"
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging.getLogger("aiohttp.access"), "disabled", True)

    def fail_authorization(_header: object, _token: object) -> bool:
        raise RuntimeError(exception_canary)

    monkeypatch.setattr(
        "codex_openai_bridge.app.bearer_is_authorized",
        fail_authorization,
    )
    app = create_app(
        _settings(tmp_path),
        StaticCredentialManager(),
        upstream=CanaryFailingUpstream(),
    )

    async with TestClient(TestServer(app, access_log=None)) as client:
        response = await client.get("/v1/models", headers=AUTH)
        body = await response.json()

    assert response.status == 500
    assert body == {
        "error": {
            "message": "Internal server error",
            "type": "server_error",
            "param": None,
            "code": "internal_error",
        }
    }
    assert exception_canary not in caplog.text + repr(body)
    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert len(records) == 1
    event = json.loads(records[0].getMessage())
    assert set(event) <= _ALLOWED_LOG_FIELDS
    assert event["endpoint"] == "models"
    assert event["status"] == 500
    assert event["code"] == "internal_error"


@pytest.mark.asyncio
async def test_logging_failure_never_changes_a_successful_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_logging(**_fields: object) -> None:
        raise RuntimeError("LOGGING_FAILURE_CANARY")

    monkeypatch.setattr(app_module, "emit_request_log", fail_logging)
    monkeypatch.setattr(logging.getLogger("aiohttp.access"), "disabled", True)
    app = create_app(
        _settings(tmp_path),
        StaticCredentialManager(),
        upstream=CanaryFailingUpstream(),
    )

    async with TestClient(TestServer(app, access_log=None)) as client:
        response = await client.get("/healthz")
        body = await response.json()

    assert response.status == 200
    assert body == {"status": "ok"}


class _MiddlewareRequest(dict[object, object]):
    method = "POST"
    path = "/v1/responses"
    content_length = None
    app: web.Application


@pytest.mark.asyncio
async def test_unexpected_failure_after_stream_preparation_never_creates_a_second_response() -> (
    None
):
    request = _MiddlewareRequest()
    prepared = web.StreamResponse(
        status=200,
        headers={"X-Request-ID": "0" * 32},
    )

    async def fail_after_prepare(_request: object) -> web.StreamResponse:
        request[app_module._PREPARED_RESPONSE_KEY] = prepared
        raise RuntimeError("PREPARED_STREAM_EXCEPTION_CANARY")

    result = await app_module._request_id_middleware(
        cast(Any, request),
        cast(Any, fail_after_prepare),
    )

    assert result is prepared
    assert result.status == 200


@pytest.mark.asyncio
async def test_logging_failure_never_masks_handler_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _MiddlewareRequest()

    async def cancel(_request: object) -> web.StreamResponse:
        raise asyncio.CancelledError

    def fail_logging(**_fields: object) -> None:
        raise RuntimeError("LOGGING_FAILURE_DURING_CANCELLATION_CANARY")

    monkeypatch.setattr(app_module, "emit_request_log", fail_logging)

    with pytest.raises(asyncio.CancelledError):
        await app_module._request_id_middleware(
            cast(Any, request),
            cast(Any, cancel),
        )


def test_cli_logging_configuration_emits_only_closed_events_and_disables_noisy_loggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_logger = logging.Logger("test.codex_bridge.requests")
    noisy_names = (
        "test.codex_bridge.aiohttp.access",
        "test.codex_bridge.aiohttp.server",
        "test.codex_bridge.httpx",
        "test.codex_bridge.httpcore",
    )
    stream = io.StringIO()
    monkeypatch.setattr(bridge_logging, "_LOGGER", request_logger)
    monkeypatch.setattr(bridge_logging, "_NOISY_LOGGER_NAMES", noisy_names)

    bridge_logging.configure_logging(stream=stream)
    bridge_logging.configure_logging(stream=stream)
    bridge_logging.emit_request_log(
        request_id="1" * 32,
        endpoint="health",
        status=200,
        duration_ms=1,
        request_bytes=0,
        response_bytes=15,
    )

    assert request_logger.level == logging.INFO
    assert request_logger.propagate is False
    assert len(request_logger.handlers) == 1
    assert json.loads(stream.getvalue()) == {
        "code": "ok",
        "duration_ms": 1,
        "endpoint": "health",
        "request_bytes": 0,
        "request_id": "1" * 32,
        "response_bytes": 15,
        "status": 200,
    }
    assert all(logging.getLogger(name).disabled for name in noisy_names)


@pytest.mark.asyncio
async def test_early_expect_rejections_emit_one_closed_event_with_the_same_request_id(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    settings = _settings(tmp_path)
    app = create_app(
        settings,
        StaticCredentialManager(),
        upstream=CanaryFailingUpstream(),
    )
    cases = (
        (
            app_module._protected_expect_handler,
            make_mocked_request(
                "POST",
                "/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                    "Expect": "100-continue",
                },
                app=app,
            ),
            401,
            "chat_completions",
        ),
        (
            app_module._protected_expect_handler,
            make_mocked_request(
                "POST",
                "/v1/responses",
                headers={
                    **AUTH,
                    "Content-Type": "application/json",
                    "Content-Length": str(settings.max_request_body_bytes + 1),
                    "Expect": "100-continue",
                },
                app=app,
            ),
            413,
            "responses",
        ),
        (
            app_module._unsupported_embeddings_expect_handler,
            make_mocked_request(
                "POST",
                "/v1/embeddings",
                headers={
                    **AUTH,
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                    "Expect": "100-continue",
                },
                app=app,
            ),
            400,
            "embeddings",
        ),
    )

    for handler, request, expected_status, expected_endpoint in cases:
        caplog.clear()
        response = await handler(request)
        assert response is not None
        assert response.status == expected_status
        records = [record for record in caplog.records if record.name == LOGGER_NAME]
        assert len(records) == 1
        event = json.loads(records[0].getMessage())
        assert set(event) <= _ALLOWED_LOG_FIELDS
        assert event["request_id"] == response.headers["X-Request-ID"]
        assert event["endpoint"] == expected_endpoint
        assert event["status"] == expected_status


@pytest.mark.asyncio
async def test_parser_error_body_and_logs_never_echo_malformed_http_bytes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_canary = "PARSER_INPUT_CANARY_64ba"
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging.getLogger("aiohttp.access"), "disabled", True)
    app = create_app(
        _settings(tmp_path),
        StaticCredentialManager(),
        upstream=CanaryFailingUpstream(),
    )
    server = TestServer(app, access_log=None)
    await server.start_server()
    try:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.write(
            (
                "GET /v1/models?secret="
                + parser_canary
                + " HTTP/1.1\r\n"
                + f"Host: {server.host}:{server.port}\r\n"
                + "Malformed Header "
                + parser_canary
                + "\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        raw_response = await asyncio.wait_for(reader.read(), timeout=1.0)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()

    assert raw_response.startswith(b"HTTP/1.0 400 ")
    assert parser_canary.encode("ascii") not in raw_response
    response_head, response_body = raw_response.split(b"\r\n\r\n", 1)
    assert b"X-Request-ID: " in response_head
    assert response_body == b"Invalid HTTP request"
    assert parser_canary not in caplog.text
    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert len(records) == 1
    event = json.loads(records[0].getMessage())
    assert set(event) <= _ALLOWED_LOG_FIELDS
    assert event["endpoint"] == "other"
    assert event["status"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [(AdmissionQueueTimeout(), 429), (AdmissionShuttingDown(), 503)],
)
async def test_early_expect_admission_rejections_are_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    expected_status: int,
) -> None:
    class RejectingAdmission:
        async def acquire(self) -> object:
            raise failure

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    app = create_app(
        _settings(tmp_path),
        StaticCredentialManager(),
        upstream=CanaryFailingUpstream(),
    )
    app[app_module._ADMISSION_KEY] = cast(Any, RejectingAdmission())
    request = make_mocked_request(
        "POST",
        "/v1/chat/completions",
        headers={
            **AUTH,
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Expect": "100-continue",
        },
        app=app,
    )

    response = await app_module._protected_expect_handler(request)

    assert response is not None
    assert response.status == expected_status
    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert len(records) == 1
    event = json.loads(records[0].getMessage())
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["endpoint"] == "chat_completions"
    assert event["status"] == expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "headers", "expected_endpoint"),
    [
        ("GET", "/unknown?secret=QUERY_EXPECT_CANARY", {}, "other"),
        ("GET", "/healthz", {}, "health"),
        ("GET", "/readyz", {}, "readiness"),
        ("GET", "/v1/models", AUTH, "models"),
        ("POST", "/healthz", {}, "other"),
    ],
)
async def test_unsupported_expect_is_generic_and_logged_before_every_route_family(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    headers: dict[str, str],
    expected_endpoint: str,
) -> None:
    expect_canary = "EXPECT_INPUT_CANARY_91ab"
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging.getLogger("aiohttp.access"), "disabled", True)
    app = create_app(
        _settings(tmp_path),
        StaticCredentialManager(),
        upstream=CanaryFailingUpstream(),
    )

    async with TestClient(TestServer(app, access_log=None)) as client:
        response = await client.request(
            method,
            path,
            headers={**headers, "Expect": expect_canary},
        )
        body = await response.text()

    assert response.status == 417
    assert body == "Unsupported expectation"
    assert len(response.headers["X-Request-ID"]) == 32
    assert expect_canary not in body + caplog.text
    assert "QUERY_EXPECT_CANARY" not in body + caplog.text
    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert len(records) == 1
    event = json.loads(records[0].getMessage())
    assert set(event) <= _ALLOWED_LOG_FIELDS
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["endpoint"] == expected_endpoint
    assert event["status"] == 417


@pytest.mark.asyncio
async def test_prepare_return_crossing_deadline_marks_authority_before_timeout_can_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosingStream:
        def __init__(self) -> None:
            self.close_calls = 0

        def __aiter__(self) -> ClosingStream:
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.close_calls += 1

    stream = ClosingStream()

    class StreamingUpstream:
        async def create_stream(self, _credential: object, _payload: object) -> ClosingStream:
            return stream

    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.1)
    app = create_app(
        settings,
        StaticCredentialManager(),
        upstream=cast(Any, StreamingUpstream()),
    )
    created: list[object] = []

    class BlockingPreparedResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            self.status = status
            self.headers = headers
            self.prepared = False
            created.append(self)

        async def prepare(self, _request: object) -> None:
            self.prepared = True
            time.sleep(0.02)

        async def write(self, _frame: bytes) -> None:
            return

    monkeypatch.setattr(web, "StreamResponse", BlockingPreparedResponse)
    request = _MiddlewareRequest()
    request.app = app

    async def frames() -> Any:
        yield b"data: first\n\n"

    def translate(_chunks: object) -> object:
        return app_module._StreamTranslation(frames(), lambda: b"data: error\n\n")

    async def handler(_request: object) -> web.StreamResponse:
        try:
            return await app_module._stream_bounded_sse(
                cast(Any, request),
                credential=cast(Any, object()),
                payload={},
                deadline=time.monotonic() + 0.02,
                translate=cast(Any, translate),
                prepare_timeout_is_upstream=True,
                request_id="a" * 32,
            )
        except UpstreamError as error:
            return app_module._upstream_error_response(error)

    result = await app_module._request_id_middleware(
        cast(Any, request),
        cast(Any, handler),
    )

    assert len(created) == 1
    committed = cast(Any, created[0])
    assert committed.prepared is True
    assert result is committed
    assert result.status == 200
    assert request[app_module._PREPARED_RESPONSE_KEY] is committed
    assert stream.close_calls == 1

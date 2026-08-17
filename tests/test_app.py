from __future__ import annotations

import asyncio
import io
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from openai.types import Model
from openai.types.chat import ChatCompletion

import codex_openai_bridge.app as app_module
from codex_openai_bridge.app import create_app
from codex_openai_bridge.auth import Credential, CredentialUnavailable
from codex_openai_bridge.config import Settings
from codex_openai_bridge.upstream import UpstreamError, UpstreamErrorKind
from codex_openai_bridge.wire import ToolCall, create_reasoning_binding_id

TOKEN = "a" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class NeverCredentialManager:
    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        del force_refresh
        raise AssertionError("credential resolver must not be called")


class UnexpectedFailingCredentialManager:
    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        del force_refresh
        raise RuntimeError("SENSITIVE_UNEXPECTED_DETAIL")


class FailingCredentialManager:
    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        del force_refresh
        raise CredentialUnavailable("SENSITIVE_RESOLVER_DETAIL")


class StaticCredentialManager:
    def __init__(self, credential: Credential) -> None:
        self.credential = credential
        self.calls: list[bool] = []

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        self.calls.append(force_refresh)
        return self.credential


class FakeUpstream:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[Credential, dict[str, Any]]] = []

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        self.calls.append((credential, payload))
        return self.response


class FakeResponsesByteStream:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        wait_after_chunks: asyncio.Event | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self._chunks = iter(chunks)
        self._wait_after_chunks = wait_after_chunks
        self._close_failure = close_failure
        self.close_calls = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration:
            if self._wait_after_chunks is not None:
                await self._wait_after_chunks.wait()
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.close_calls += 1
        if self._close_failure is not None:
            raise self._close_failure


class StreamingFakeUpstream:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        wait_after_chunks: asyncio.Event | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self.stream = FakeResponsesByteStream(
            chunks,
            wait_after_chunks=wait_after_chunks,
            close_failure=close_failure,
        )
        self.calls: list[tuple[Credential, dict[str, Any]]] = []

    async def create_stream(
        self, credential: Credential, payload: dict[str, Any]
    ) -> FakeResponsesByteStream:
        self.calls.append((credential, payload))
        return self.stream


def _sse_event(value: object) -> bytes:
    return b"data: " + json.dumps(value, separators=(",", ":")).encode() + b"\n\n"


def _stream_text_events() -> list[dict[str, object]]:
    added_message = {
        "id": "msg_stream",
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    done_message = {
        **added_message,
        "status": "completed",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    }
    return [
        {
            "type": "response.created",
            "response": {"id": "resp_stream", "created_at": 7, "status": "in_progress"},
        },
        {"type": "response.output_item.added", "output_index": 0, "item": added_message},
        {
            "type": "response.content_part.added",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "delta": "hello",
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "text": "hello",
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "hello", "annotations": []},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done_message},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_stream",
                "created_at": 7,
                "status": "completed",
                "output": [done_message],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            },
        },
    ]


class SequenceFakeUpstream(FakeUpstream):
    def __init__(self, responses: list[object]) -> None:
        super().__init__({})
        self.responses = responses

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        self.calls.append((credential, payload))
        return self.responses[len(self.calls) - 1]


class FailingUpstream:
    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        del credential, payload
        raise RuntimeError("SENSITIVE_UPSTREAM_DETAIL")


class CloseTrackingUpstream(FakeUpstream):
    def __init__(self) -> None:
        super().__init__({})
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class CategorizedFailingUpstream:
    def __init__(self, error: UpstreamError) -> None:
        self.error = error

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        del credential, payload
        raise self.error


def _credential(
    *,
    access_token: str = "upstream-token",
    base_url: str = "https://chatgpt.com/backend-api/codex",
    account_id: str | None = "account-1",
    expires_at: int = 4_102_444_800,
) -> Credential:
    return Credential(
        access_token=access_token,
        base_url=base_url,
        account_id=account_id,
        expires_at=expires_at,
    )


def _settings(tmp_path: Path) -> Settings:
    token_file = tmp_path / "client-token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    return replace(Settings.from_env(), client_token_file=token_file)


@pytest.mark.parametrize(
    "error,status,code,retry_after",
    [
        (UpstreamError(UpstreamErrorKind.SERVICE), 502, "upstream_error", None),
        (UpstreamError(UpstreamErrorKind.TIMEOUT), 504, "upstream_timeout", None),
        (
            UpstreamError(UpstreamErrorKind.CREDENTIALS),
            503,
            "credentials_unavailable",
            None,
        ),
        (
            UpstreamError(UpstreamErrorKind.RATE_LIMIT, retry_after="120"),
            429,
            "rate_limit_exceeded",
            "120",
        ),
        (
            UpstreamError(UpstreamErrorKind.RATE_LIMIT),
            429,
            "rate_limit_exceeded",
            None,
        ),
        (
            UpstreamError(
                UpstreamErrorKind.RATE_LIMIT,
                retry_after="999999999999999999999999",
            ),
            429,
            "rate_limit_exceeded",
            None,
        ),
    ],
)
def test_upstream_error_mapping_is_openai_shaped_and_bounded(
    error: UpstreamError,
    status: int,
    code: str,
    retry_after: str | None,
) -> None:
    response = app_module._upstream_error_response(error)
    assert isinstance(response.body, (bytes, bytearray))
    body = json.loads(response.body)

    assert response.status == status
    assert body["error"]["code"] == code
    assert body["error"]["param"] is None
    assert response.headers.get("Retry-After") == retry_after
    assert "upstream request failed" not in repr(body)


@pytest.mark.asyncio
async def test_authenticated_text_chat_completion_tracer_bullet(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    credential = _credential()
    manager = StaticCredentialManager(credential)
    upstream = FakeUpstream(
        {
            "id": "resp_test",
            "status": "completed",
            "created_at": 1_723_456_789,
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello back"}],
                }
            ],
            "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
        }
    )
    app = create_app(settings, manager, upstream=upstream)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "codex", "messages": [{"role": "user", "content": "hello"}]},
            headers=AUTH,
        )
        body = await response.json()

    assert response.status == 200
    assert body["choices"][0]["message"]["content"] == "hello back"
    assert manager.calls == [False]
    assert upstream.calls == [
        (
            credential,
            {
                "model": settings.upstream_model,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
                "store": False,
                "stream": False,
                "include": ["reasoning.encrypted_content"],
            },
        )
    ]


@pytest.mark.asyncio
async def test_streaming_text_prefetches_then_emits_sse_usage_and_done(tmp_path: Path) -> None:
    wire = b"".join(_sse_event(event) for event in _stream_text_events())
    upstream = StreamingFakeUpstream([wire + b"data: [DONE]\n\n"])
    credential = _credential()
    manager = StaticCredentialManager(credential)
    app = create_app(_settings(tmp_path), manager, upstream=upstream)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "codex",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            headers=AUTH,
        )
        body = await response.read()

    assert response.status == 200
    assert response.headers["Content-Type"] == "text/event-stream; charset=utf-8"
    assert b'"role":"assistant"' in body
    assert b'"content":"hello"' in body
    assert b'"choices":[],"usage":{"prompt_tokens":1' in body
    assert body.endswith(b"data: [DONE]\n\n")
    assert manager.calls == [False]
    assert upstream.calls[0][0] == credential
    assert upstream.calls[0][1]["stream"] is True
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_stream_failure_before_first_frame_returns_json_error(tmp_path: Path) -> None:
    secret = "SENSITIVE_UPSTREAM_FAILURE"
    upstream = StreamingFakeUpstream(
        [_sse_event({"type": "response.failed", "response": {"error": secret}})]
    )
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential()), upstream=upstream)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "codex",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers=AUTH,
        )
        body = await response.read()

    assert response.status == 502
    assert response.headers["Content-Type"].startswith("application/json")
    assert b"upstream_error" in body
    assert secret.encode() not in body
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_stream_malformed_after_first_frame_emits_one_error_and_no_done(
    tmp_path: Path,
) -> None:
    events = _stream_text_events()[:4]
    first = b"".join(_sse_event(event) for event in events)
    secret = b"SENSITIVE_MALFORMED_VALUE"
    upstream = StreamingFakeUpstream([first, b"data: {}\ndata: " + secret + b"\n\n"])
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential()), upstream=upstream)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "codex",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers=AUTH,
        )
        body = await response.read()

    assert response.status == 200
    assert body.count(b'"code":"upstream_stream_error"') == 1
    assert b"data: [DONE]" not in body
    assert secret not in body
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_stream_write_cancellation_propagates_and_closes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = b"".join(_sse_event(event) for event in _stream_text_events()[:4])
    upstream = StreamingFakeUpstream([first])
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential()), upstream=upstream)

    class CancellingResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            del frame
            raise asyncio.CancelledError

    monkeypatch.setattr(web, "StreamResponse", CancellingResponse)
    request = make_mocked_request(
        "POST",
        "/v1/chat/completions",
        headers=AUTH,
        app=app,
    )

    async def stream_handler(stream_request: web.Request) -> web.StreamResponse:
        return await app_module._stream_chat_completion(
            stream_request,
            credential=_credential(),
            payload={"model": "server-model", "stream": True},
            include_usage=False,
            deadline=time.monotonic() + 1,
        )

    with pytest.raises(asyncio.CancelledError):
        await app_module._admission_middleware(request, stream_handler)

    assert upstream.stream.close_calls == 1
    assert app[app_module._ADMISSION_KEY].active_count == 0
    assert app[app_module._ADMISSION_KEY].waiting_count == 0


@pytest.mark.asyncio
async def test_stream_protocol_failure_after_prefetch_writes_one_terminal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = b"".join(_sse_event(event) for event in _stream_text_events()[:4])
    upstream = StreamingFakeUpstream([first, b"data: {}\ndata: duplicate\n\n"])
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential()), upstream=upstream)
    writes: list[bytes] = []

    class CollectingResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            assert status == 200
            assert headers["Content-Type"] == "text/event-stream; charset=utf-8"

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", CollectingResponse)
    request = cast(Any, SimpleNamespace(app=app))

    await app_module._stream_chat_completion(
        request,
        credential=_credential(),
        payload={"model": "server-model", "stream": True},
        include_usage=False,
        deadline=time.monotonic() + 1,
    )

    combined = b"".join(writes)
    assert b'"role":"assistant"' in combined
    assert combined.count(b'"code":"upstream_stream_error"') == 1
    assert b"data: [DONE]" not in combined
    assert b"duplicate" not in combined
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_stream_total_timeout_after_first_frame_uses_reserved_terminal_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = b"".join(_sse_event(event) for event in _stream_text_events()[:4])
    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.1)
    upstream = StreamingFakeUpstream([first], wait_after_chunks=asyncio.Event())
    app = create_app(settings, StaticCredentialManager(_credential()), upstream=upstream)
    writes: list[bytes] = []

    class CollectingResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", CollectingResponse)
    request = cast(Any, SimpleNamespace(app=app))

    await app_module._stream_chat_completion(
        request,
        credential=_credential(),
        payload={"model": "server-model", "stream": True},
        include_usage=False,
        deadline=time.monotonic() + settings.total_request_deadline_seconds,
    )

    combined = b"".join(writes)
    assert b'"role":"assistant"' in combined
    assert combined.count(b'"code":"upstream_stream_error"') == 1
    assert b"data: [DONE]" not in combined
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_terminal_budget_is_reserved_from_original_deadline_before_slow_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = b"".join(_sse_event(event) for event in _stream_text_events()[:4])
    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.2)
    upstream = StreamingFakeUpstream([first], wait_after_chunks=asyncio.Event())
    app = create_app(settings, StaticCredentialManager(_credential()), upstream=upstream)
    writes: list[bytes] = []

    class SlowPrepareAndTerminalWriteResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request
            await asyncio.sleep(0.15)

        async def write(self, frame: bytes) -> None:
            if frame == app_module._STREAM_ERROR_FRAME:
                await asyncio.sleep(0.01)
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", SlowPrepareAndTerminalWriteResponse)
    request = cast(Any, SimpleNamespace(app=app))

    await app_module._stream_chat_completion(
        request,
        credential=_credential(),
        payload={"model": "server-model", "stream": True},
        include_usage=False,
        deadline=time.monotonic() + settings.total_request_deadline_seconds,
    )

    combined = b"".join(writes)
    assert combined.count(b'"code":"upstream_stream_error"') == 1
    assert b"data: [DONE]" not in combined
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_stream_chunk_write_timeout_after_first_frame_uses_terminal_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = b"".join(_sse_event(event) for event in _stream_text_events())
    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.1)
    upstream = StreamingFakeUpstream([wire + b"data: [DONE]\n\n"])
    app = create_app(settings, StaticCredentialManager(_credential()), upstream=upstream)
    writes: list[bytes] = []
    write_calls = 0

    class SlowSecondWriteResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            nonlocal write_calls
            write_calls += 1
            if write_calls == 2:
                await asyncio.Event().wait()
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", SlowSecondWriteResponse)
    request = cast(Any, SimpleNamespace(app=app))

    await app_module._stream_chat_completion(
        request,
        credential=_credential(),
        payload={"model": "server-model", "stream": True},
        include_usage=False,
        deadline=time.monotonic() + settings.total_request_deadline_seconds,
    )

    combined = b"".join(writes)
    assert b'"role":"assistant"' in combined
    assert combined.count(b'"code":"upstream_stream_error"') == 1
    assert b"data: [DONE]" not in combined
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_handled_stream_error_is_not_replaced_by_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = b"".join(_sse_event(event) for event in _stream_text_events()[:4])
    upstream = StreamingFakeUpstream(
        [first, b"data: {}\ndata: duplicate\n\n"],
        close_failure=RuntimeError("SENSITIVE_CLOSE_FAILURE"),
    )
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential()), upstream=upstream)
    writes: list[bytes] = []

    class CollectingResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", CollectingResponse)
    request = cast(Any, SimpleNamespace(app=app))

    await app_module._stream_chat_completion(
        request,
        credential=_credential(),
        payload={"model": "server-model", "stream": True},
        include_usage=False,
        deadline=time.monotonic() + 1,
    )

    combined = b"".join(writes)
    assert combined.count(b'"code":"upstream_stream_error"') == 1
    assert b"data: [DONE]" not in combined
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_completed_stream_close_failure_does_not_escape_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = b"".join(_sse_event(event) for event in _stream_text_events())
    upstream = StreamingFakeUpstream(
        [wire + b"data: [DONE]\n\n"],
        close_failure=RuntimeError("SENSITIVE_CLOSE_FAILURE"),
    )
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential()), upstream=upstream)
    writes: list[bytes] = []

    class CollectingResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", CollectingResponse)
    request = cast(Any, SimpleNamespace(app=app))

    await app_module._stream_chat_completion(
        request,
        credential=_credential(),
        payload={"model": "server-model", "stream": True},
        include_usage=False,
        deadline=time.monotonic() + 1,
    )

    assert writes[-1] == b"data: [DONE]\n\n"
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_stream_request_id_header_is_installed_before_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = b"".join(_sse_event(event) for event in _stream_text_events())
    upstream = StreamingFakeUpstream([wire + b"data: [DONE]\n\n"])
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential()), upstream=upstream)
    prepared_headers: dict[str, str] = {}

    class HeaderCapturingResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status
            self.headers = headers

        async def prepare(self, request: object) -> None:
            del request
            prepared_headers.update(self.headers)

        async def write(self, frame: bytes) -> None:
            del frame

    monkeypatch.setattr(web, "StreamResponse", HeaderCapturingResponse)
    request = cast(Any, SimpleNamespace(app=app))

    await app_module._stream_chat_completion(
        request,
        credential=_credential(),
        payload={"model": "server-model", "stream": True},
        include_usage=False,
        deadline=time.monotonic() + 1,
        request_id="b" * 32,
    )

    assert prepared_headers["X-Request-ID"] == "b" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_format", "expected_text"),
    [
        (
            {"type": "json_object"},
            {"format": {"type": "json_object"}},
        ),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer_result",
                    "description": "One answer",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            {
                "format": {
                    "type": "json_schema",
                    "name": "answer_result",
                    "description": "One answer",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            },
        ),
    ],
)
async def test_structured_output_reaches_injected_upstream_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_format: dict[str, object],
    expected_text: dict[str, object],
) -> None:
    settings = _settings(tmp_path)
    credential = _credential()
    manager = StaticCredentialManager(credential)
    upstream = FakeUpstream(
        {
            "id": "resp_structured",
            "status": "completed",
            "created_at": 1_723_456_789,
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"answer":"yes"}'}],
                }
            ],
            "usage": {"input_tokens": 2, "output_tokens": 4, "total_tokens": 6},
        }
    )
    app = create_app(settings, manager, upstream=upstream)
    document = {
        "model": "codex",
        "messages": [{"role": "user", "content": "answer"}],
        "response_format": response_format,
    }

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return document

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._chat_completions(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))
    body = json.loads(response.body)

    assert response.status == 200
    assert body["choices"][0]["message"]["content"] == '{"answer":"yes"}'
    assert upstream.calls == [
        (
            credential,
            {
                "model": settings.upstream_model,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "answer"}],
                    }
                ],
                "store": False,
                "stream": False,
                "include": ["reasoning.encrypted_content"],
                "text": expected_text,
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings_overrides", "schema"),
    [
        ({}, {"patternProperties": {}}),
        ({"max_json_depth": 2}, {"anyOf": [{}]}),
        ({"max_json_nodes": 3}, {"anyOf": [{}, {}]}),
        ({"max_string_bytes": 5}, {"title": "aaaaaa"}),
    ],
)
async def test_invalid_or_overbound_schema_is_sanitized_before_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings_overrides: dict[str, int],
    schema: dict[str, object],
) -> None:
    settings = replace(_settings(tmp_path), **cast(Any, settings_overrides))
    app = create_app(settings, NeverCredentialManager(), upstream=FakeUpstream({}))
    document = {
        "model": "codex",
        "messages": [{"role": "user", "content": "answer"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "r", "schema": schema},
        },
    }

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return document

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._chat_completions(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 400
    assert json.loads(response.body) == {
        "error": {
            "message": "Request is invalid",
            "type": "invalid_request_error",
            "param": None,
            "code": "invalid_request",
        }
    }


@pytest.mark.asyncio
async def test_two_request_function_tool_round_trip_uses_injected_upstream_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    credential = _credential()
    manager = StaticCredentialManager(credential)
    upstream = SequenceFakeUpstream(
        [
            {
                "id": "resp_call",
                "status": "completed",
                "created_at": 1_723_456_789,
                "output": [
                    {
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_weather",
                        "name": "lookup_weather",
                        "arguments": '{"city":"Tokyo"}',
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
            },
            {
                "id": "resp_final",
                "status": "completed",
                "created_at": 1_723_456_790,
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "It is sunny."}],
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
            },
        ]
    )
    app = create_app(settings, manager, upstream=upstream)
    tool = {
        "type": "function",
        "function": {
            "name": "lookup_weather",
            "description": "Look up weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            "strict": True,
        },
    }
    documents = [
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "Weather?"}],
            "tools": [tool],
            "tool_choice": "required",
            "parallel_tool_calls": True,
        },
        {
            "model": "codex",
            "messages": [
                {"role": "user", "content": "Weather?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "lookup_weather",
                                "arguments": '{"city":"Tokyo"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_weather",
                    "content": '{"condition":"sunny"}',
                },
            ],
            "tools": [tool],
        },
    ]

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return documents.pop(0)

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    request = cast(Any, SimpleNamespace(app=app))
    first_response = await app_module._chat_completions(request)
    second_response = await app_module._chat_completions(request)
    assert isinstance(first_response.body, (bytes, bytearray))
    assert isinstance(second_response.body, (bytes, bytearray))
    first_body = json.loads(first_response.body)
    second_body = json.loads(second_response.body)

    assert first_response.status == 200
    assert first_body["choices"][0] == {
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "arguments": '{"city":"Tokyo"}',
                    },
                }
            ],
        },
        "finish_reason": "tool_calls",
    }
    assert second_response.status == 200
    assert second_body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "It is sunny.",
    }
    assert manager.calls == [False, False]
    assert upstream.calls[0] == (
        credential,
        {
            "model": settings.upstream_model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Weather?"}],
                }
            ],
            "store": False,
            "stream": False,
            "include": ["reasoning.encrypted_content"],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup_weather",
                    "description": "Look up weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                    "strict": True,
                }
            ],
            "tool_choice": "required",
            "parallel_tool_calls": True,
        },
    )
    assert upstream.calls[1][1]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "Weather?"}]},
        {
            "type": "function_call",
            "call_id": "call_weather",
            "name": "lookup_weather",
            "arguments": '{"city":"Tokyo"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_weather",
            "output": '{"condition":"sunny"}',
        },
    ]


@pytest.mark.asyncio
async def test_two_call_encrypted_reasoning_round_trip_survives_sdk_and_replays_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    credential = _credential()
    manager = StaticCredentialManager(credential)
    upstream = SequenceFakeUpstream(
        [
            {
                "id": "resp_reasoning",
                "status": "completed",
                "created_at": 1_723_456_789,
                "output": [
                    {
                        "type": "reasoning",
                        "id": "raw-secret-id",
                        "status": "completed",
                        "summary": [{"type": "summary_text", "text": "PRIVATE SUMMARY"}],
                        "encrypted_content": "YQ==",
                    },
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "First answer"}],
                    },
                ],
                "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
            },
            {
                "id": "resp_final",
                "status": "completed",
                "created_at": 1_723_456_790,
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Second answer"}],
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            },
        ]
    )
    app = create_app(settings, manager, upstream=upstream)
    documents: list[dict[str, object]] = [
        {"model": "codex", "messages": [{"role": "user", "content": "first"}]}
    ]

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return documents.pop(0)

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    request = cast(Any, SimpleNamespace(app=app))
    first_response = await app_module._chat_completions(request)
    assert isinstance(first_response.body, (bytes, bytearray))
    first_body = json.loads(first_response.body)
    sdk_message = ChatCompletion.model_validate(first_body).choices[0].message.model_dump()
    detail = sdk_message["reasoning_details"][0]
    assert detail == {
        "type": "reasoning.encrypted",
        "data": "YQ==",
        "format": "openai-responses-v1",
        "id": detail["id"],
        "index": 0,
    }
    assert detail["id"].startswith("cobr_r2_")
    assert "PRIVATE" not in repr(first_body)
    assert "raw-secret-id" not in repr(first_body)

    # This is the exact assistant subset current Honcho places back into history.
    honcho_assistant = {
        "role": "assistant",
        "content": sdk_message["content"],
        "reasoning_details": sdk_message["reasoning_details"],
    }
    documents.append(
        {
            "model": "codex",
            "messages": [
                {"role": "user", "content": "first"},
                honcho_assistant,
                {"role": "user", "content": "continue"},
            ],
        }
    )
    second_response = await app_module._chat_completions(request)

    assert second_response.status == 200
    assert manager.calls == [False, False]
    assert upstream.calls[1][1]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
        {"type": "reasoning", "encrypted_content": "YQ=="},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "First answer"}],
        },
        {"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
    ]


@pytest.mark.asyncio
async def test_reasoning_move_tool_tamper_and_cross_message_duplicate_fail_before_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, NeverCredentialManager(), upstream=FakeUpstream({}))

    def detail_for(content: str | None, calls: tuple[ToolCall, ...] = ()) -> dict[str, object]:
        data = "YQ=="
        return {
            "type": "reasoning.encrypted",
            "data": data,
            "format": "openai-responses-v1",
            "id": create_reasoning_binding_id(
                binding_key=TOKEN,
                content=content,
                tool_calls=calls,
                index=0,
                data=data,
            ),
            "index": 0,
        }

    original = detail_for("original")
    original_calls = (ToolCall(call_id="call_1", name="original", arguments="{}"),)
    call_detail = detail_for(None, original_calls)
    documents: list[dict[str, object]] = [
        {
            "model": "codex",
            "messages": [
                {"role": "assistant", "content": "moved", "reasoning_details": [original]}
            ],
        },
        {
            "model": "codex",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_details": [call_detail],
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "changed", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            ],
        },
        {
            "model": "codex",
            "messages": [
                {"role": "assistant", "content": "original", "reasoning_details": [original]},
                {"role": "user", "content": "continue"},
                {"role": "assistant", "content": "original", "reasoning_details": [original]},
            ],
        },
    ]

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return documents.pop(0)

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    request = cast(Any, SimpleNamespace(app=app))
    for _ in range(3):
        response = await app_module._chat_completions(request)
        assert isinstance(response.body, (bytes, bytearray))
        assert response.status == 400
        assert json.loads(response.body)["error"]["message"] == "Request is invalid"
        assert TOKEN not in response.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["bad_base64", "oversized", "deep", "explicit_null", "duplicate_blob"]
)
async def test_bad_or_overbound_upstream_reasoning_is_sanitized_502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    settings = replace(_settings(tmp_path), max_json_depth=4, max_string_bytes=64)
    reasoning: dict[str, object] = {"type": "reasoning", "encrypted_content": "YQ=="}
    additional_reasoning: list[dict[str, object]] = []
    if case == "bad_base64":
        reasoning["encrypted_content"] = "SENSITIVE BAD BLOB"
    elif case == "oversized":
        reasoning["encrypted_content"] = "Y" * 65
    elif case == "deep":
        reasoning["unknown"] = {"nested": {"too": "deep"}}
    elif case == "explicit_null":
        reasoning["encrypted_content"] = None
    else:
        reasoning["encrypted_content"] = "++8="
        additional_reasoning.append({"type": "reasoning", "encrypted_content": "--8"})
    upstream = FakeUpstream(
        {
            "id": "resp_bad",
            "status": "completed",
            "created_at": 1,
            "output": [
                reasoning,
                *additional_reasoning,
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
    )
    app = create_app(settings, StaticCredentialManager(_credential()), upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "messages": [{"role": "user", "content": "x"}]}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._chat_completions(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 502
    assert json.loads(response.body)["error"]["message"] == "Upstream service unavailable"
    assert "SENSITIVE" not in response.body.decode()
    assert TOKEN not in response.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "tools": [
                {"type": "function", "function": {"name": "one", "parameters": {}}},
                {"type": "function", "function": {"name": "two", "parameters": {}}},
            ],
        },
        {
            "model": "codex",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_secret",
                            "type": "function",
                            "function": {"name": "f", "arguments": "SENSITIVE_ARGUMENTS"},
                        }
                    ],
                }
            ],
        },
    ],
)
async def test_invalid_tool_request_is_sanitized_before_credentials_direct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: object,
) -> None:
    settings = replace(_settings(tmp_path), max_tools=1)
    app = create_app(settings, NeverCredentialManager(), upstream=FakeUpstream({}))

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return document

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._chat_completions(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))
    body = json.loads(response.body)

    assert response.status == 400
    assert body["error"]["code"] == "invalid_request"
    assert "SENSITIVE_ARGUMENTS" not in repr(body)


@pytest.mark.asyncio
async def test_malformed_upstream_tool_call_is_sanitized_502_direct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = StaticCredentialManager(_credential())
    upstream = FakeUpstream(
        {
            "id": "resp_bad",
            "status": "completed",
            "created_at": 1,
            "output": [
                {
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_1",
                    "name": "f",
                    "arguments": '{"secret":NaN}',
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
    )
    app = create_app(_settings(tmp_path), manager, upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "messages": [{"role": "user", "content": "text"}]}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._chat_completions(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))
    body = json.loads(response.body)

    assert response.status == 502
    assert body["error"]["code"] == "upstream_error"
    assert "secret" not in repr(body)
    assert "secret" not in caplog.text
    assert manager.calls == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"model": "other", "messages": [{"role": "user", "content": "text"}]},
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "stream_options": {"include_usage": True},
        },
    ],
)
async def test_invalid_task_6_request_is_sanitized_before_credentials(
    tmp_path: Path,
    payload: object,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=FakeUpstream({}))

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=payload,
            headers=AUTH,
        )
        body = await response.json()

    assert response.status == 400
    assert body == {
        "error": {
            "message": "Request is invalid",
            "type": "invalid_request_error",
            "param": None,
            "code": "invalid_request",
        }
    }
    assert "other" not in repr(body)


@pytest.mark.asyncio
async def test_upstream_exception_becomes_sanitized_service_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = StaticCredentialManager(_credential())
    app = create_app(_settings(tmp_path), manager, upstream=FailingUpstream())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "codex", "messages": [{"role": "user", "content": "text"}]},
            headers=AUTH,
        )
        body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "upstream_error"
    assert "SENSITIVE_UPSTREAM_DETAIL" not in repr(body)
    assert "SENSITIVE_UPSTREAM_DETAIL" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status,code,retry_after",
    [
        (UpstreamError(UpstreamErrorKind.SERVICE), 502, "upstream_error", None),
        (UpstreamError(UpstreamErrorKind.TIMEOUT), 504, "upstream_timeout", None),
        (
            UpstreamError(UpstreamErrorKind.CREDENTIALS),
            503,
            "credentials_unavailable",
            None,
        ),
        (
            UpstreamError(UpstreamErrorKind.RATE_LIMIT, retry_after="9"),
            429,
            "rate_limit_exceeded",
            "9",
        ),
    ],
)
async def test_categorized_upstream_error_preserves_request_id(
    tmp_path: Path,
    error: UpstreamError,
    status: int,
    code: str,
    retry_after: str | None,
) -> None:
    manager = StaticCredentialManager(_credential())
    app = create_app(
        _settings(tmp_path),
        manager,
        upstream=CategorizedFailingUpstream(error),
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "codex", "messages": [{"role": "user", "content": "text"}]},
            headers=AUTH,
        )
        body = await response.json()

    assert response.status == status
    assert body["error"]["code"] == code
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])
    assert response.headers.get("Retry-After") == retry_after
    assert manager.calls == [False]


@pytest.mark.asyncio
async def test_chat_credential_resolver_failure_is_sanitized_503(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        _settings(tmp_path),
        UnexpectedFailingCredentialManager(),
        upstream=FakeUpstream({}),
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "codex", "messages": [{"role": "user", "content": "text"}]},
            headers=AUTH,
        )
        body = await response.json()

    assert response.status == 503
    assert body["error"]["code"] == "credentials_unavailable"
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])
    assert "SENSITIVE_UNEXPECTED_DETAIL" not in repr(body)
    assert "SENSITIVE_UNEXPECTED_DETAIL" not in caplog.text


@pytest.mark.asyncio
async def test_malformed_upstream_response_becomes_sanitized_service_error(tmp_path: Path) -> None:
    manager = StaticCredentialManager(_credential())
    app = create_app(
        _settings(tmp_path),
        manager,
        upstream=FakeUpstream({"status": "completed", "output": "SENSITIVE_BODY_VALUE"}),
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "codex", "messages": [{"role": "user", "content": "text"}]},
            headers=AUTH,
        )
        body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "upstream_error"
    assert "SENSITIVE_BODY_VALUE" not in repr(body)


@pytest.mark.asyncio
async def test_injected_upstream_is_not_owned_or_closed(tmp_path: Path) -> None:
    upstream = CloseTrackingUpstream()
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=upstream)
    app.freeze()

    await app.startup()
    await app.cleanup()

    assert upstream.close_calls == 0


@pytest.mark.asyncio
async def test_application_closes_its_owned_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = CloseTrackingUpstream()
    monkeypatch.setattr(
        app_module,
        "HttpxResponsesUpstream",
        lambda _settings, **_kwargs: upstream,
    )
    app = create_app(_settings(tmp_path), NeverCredentialManager())
    app.freeze()

    await app.startup()
    await app.cleanup()

    assert upstream.close_calls == 1


@pytest.mark.asyncio
async def test_owned_upstream_receives_narrow_forced_credential_refresher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential(access_token="refreshed-token")
    manager = StaticCredentialManager(credential)
    upstream = CloseTrackingUpstream()
    captured: dict[str, Any] = {}

    def factory(
        _settings: Settings,
        *,
        credential_refresher: Any,
    ) -> CloseTrackingUpstream:
        captured["credential_refresher"] = credential_refresher
        return upstream

    monkeypatch.setattr(app_module, "HttpxResponsesUpstream", factory)
    app = create_app(_settings(tmp_path), manager)
    app.freeze()

    refreshed = await captured["credential_refresher"]()
    await app.startup()
    await app.cleanup()

    assert refreshed is credential
    assert manager.calls == [True]
    assert upstream.close_calls == 1


@pytest.mark.asyncio
async def test_expect_100_wrong_content_type_is_rejected_before_continue(tmp_path: Path) -> None:
    server = TestServer(create_app(_settings(tmp_path), NeverCredentialManager()))

    async with server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {server.host}:{server.port}\r\n"
                    "Content-Length: 2\r\n"
                    "Content-Type: text/plain\r\n"
                    "Expect: 100-continue\r\n"
                    f"Authorization: Bearer {TOKEN}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            first_response_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        finally:
            writer.close()
            await writer.wait_closed()

    assert first_response_head.startswith(b"HTTP/1.1 400 ")
    assert b"100 Continue" not in first_response_head


@pytest.mark.asyncio
async def test_expect_100_authorized_oversize_is_rejected_before_continue(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), max_request_body_bytes=1024)
    server = TestServer(create_app(settings, NeverCredentialManager()))

    async with server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {server.host}:{server.port}\r\n"
                    f"Content-Length: {settings.max_request_body_bytes + 1}\r\n"
                    "Content-Type: application/json\r\n"
                    "Expect: 100-continue\r\n"
                    f"Authorization: Bearer {TOKEN}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            first_response_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        finally:
            writer.close()
            await writer.wait_closed()

    assert first_response_head.startswith(b"HTTP/1.1 413 ")
    assert b"100 Continue" not in first_response_head


@pytest.mark.asyncio
async def test_expect_100_authorized_request_preserves_continue_flow(tmp_path: Path) -> None:
    server = TestServer(create_app(_settings(tmp_path), NeverCredentialManager()))

    async with server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {server.host}:{server.port}\r\n"
                    "Content-Length: 2\r\n"
                    "Content-Type: application/json\r\n"
                    "Expect: 100-continue\r\n"
                    f"Authorization: Bearer {TOKEN}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            continue_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
            writer.write(b"{}")
            await writer.drain()
            final_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        finally:
            writer.close()
            await writer.wait_closed()

    assert continue_head == b"HTTP/1.1 100 Continue\r\n\r\n"
    assert final_head.startswith(b"HTTP/1.1 400 ")
    assert re.search(rb"\r\nX-Request-ID: [0-9a-f]{32}\r\n", final_head, re.IGNORECASE)


@pytest.mark.asyncio
async def test_expect_100_unauthorized_first_wire_response_is_401(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    server = TestServer(create_app(settings, NeverCredentialManager()))

    async with server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {server.host}:{server.port}\r\n"
                    f"Content-Length: {settings.max_request_body_bytes + 1}\r\n"
                    "Expect: 100-continue\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            first_response_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        finally:
            writer.close()
            await writer.wait_closed()

    assert first_response_head.startswith(b"HTTP/1.1 401 ")
    assert b"100 Continue" not in first_response_head
    assert re.search(rb"\r\nX-Request-ID: [0-9a-f]{32}\r\n", first_response_head, re.IGNORECASE)


@pytest.mark.asyncio
async def test_expect_100_unauthorized_request_never_starts_body_upload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, NeverCredentialManager())
    body_starts = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal body_starts
        body_starts += 1
        yield b"x" * (settings.max_request_body_bytes + 1)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=body(),
            expect100=True,
        )

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert body_starts == 0


@pytest.mark.asyncio
async def test_duplicate_authorization_headers_are_rejected(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())
    headers = [
        ("Authorization", f"Bearer {TOKEN}"),
        ("Authorization", "Bearer " + "b" * 43),
    ]

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", data=b"{}", headers=headers)

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic abc", "Bearer wrong", "bearer " + TOKEN, "Bearer " + "b" * 43],
)
async def test_missing_malformed_or_wrong_bearer_returns_openai_401(
    tmp_path: Path,
    authorization: str | None,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())
    headers = {} if authorization is None else {"Authorization": authorization}

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", data=b"{}", headers=headers)
        body = await response.json()

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert body == {
        "error": {
            "message": "Invalid authentication credentials",
            "type": "invalid_request_error",
            "param": None,
            "code": "invalid_api_key",
        }
    }
    assert TOKEN not in repr(body)


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_without_waiting_for_body(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), max_request_body_bytes=1024)
    server = TestServer(create_app(settings, NeverCredentialManager()))

    async with server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {server.host}:{server.port}\r\n"
                    f"Content-Length: {settings.max_request_body_bytes + 1}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Authorization: Bearer {TOKEN}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            response_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
        finally:
            writer.close()
            await writer.wait_closed()

    assert response_head.startswith(b"HTTP/1.1 413 ")


@pytest.mark.asyncio
async def test_truncated_content_length_never_reaches_application_handler(tmp_path: Path) -> None:
    server = TestServer(create_app(_settings(tmp_path), NeverCredentialManager()))

    async with server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {server.host}:{server.port}\r\n"
                    "Content-Length: 10\r\n"
                    "Content-Type: application/json\r\n"
                    f"Authorization: Bearer {TOKEN}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                    "{}"
                ).encode("ascii")
            )
            await writer.drain()
            writer.write_eof()
            try:
                response_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
            except asyncio.IncompleteReadError as exc:
                response_head = exc.partial
        finally:
            writer.close()
            await writer.wait_closed()

    assert not response_head.startswith(b"HTTP/1.1 200 ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/json", "Application/JSON; Charset=UTF-8", 'application/json; charset="utf-8"'],
)
async def test_supported_json_content_type_is_accepted(
    tmp_path: Path,
    content_type: str,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=b"{}",
            headers={**AUTH, "Content-Type": content_type},
        )
        body = await response.json()

    assert response.status == 400
    assert body["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_duplicate_content_type_headers_are_rejected(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())
    headers = [
        ("Authorization", f"Bearer {TOKEN}"),
        ("Content-Type", "text/plain"),
        ("Content-Type", "application/json"),
    ]

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", data=b"{}", headers=headers)

    assert response.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "application/problem+json",
        "application/json; charset=iso-8859-1",
        "application/json; profile=example",
    ],
)
async def test_wrong_json_content_type_is_rejected(
    tmp_path: Path,
    content_type: str,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=b"{}",
            headers={**AUTH, "Content-Type": content_type},
        )

    assert response.status == 400


@pytest.mark.asyncio
async def test_authenticated_body_exact_limit_is_accepted(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        max_request_body_bytes=1024,
        max_string_bytes=1024,
    )
    payload = b'{"x":"' + b"a" * (settings.max_request_body_bytes - 8) + b'"}'
    assert len(payload) == settings.max_request_body_bytes
    app = create_app(settings, NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=payload,
            headers={**AUTH, "Content-Type": "application/json"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_authenticated_content_length_one_over_is_413(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        max_request_body_bytes=1024,
        max_string_bytes=1024,
    )
    payload = b'{"x":"' + b"a" * (settings.max_request_body_bytes - 7) + b'"}'
    assert len(payload) == settings.max_request_body_bytes + 1
    app = create_app(settings, NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=payload,
            headers={**AUTH, "Content-Type": "application/json"},
        )
        body = await response.json()

    assert response.status == 413
    assert body["error"]["code"] == "request_too_large"
    assert "aaaa" not in repr(body)


@pytest.mark.asyncio
async def test_authenticated_chunked_body_one_over_is_413(tmp_path: Path) -> None:
    settings = replace(
        _settings(tmp_path),
        max_request_body_bytes=1024,
        max_string_bytes=1024,
    )
    app = create_app(settings, NeverCredentialManager())

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"x":"'
        yield b"a" * (settings.max_request_body_bytes - 7)
        yield b'"}'

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=chunks(),
            headers={**AUTH, "Content-Type": "application/json"},
        )

    assert response.status == 413


@pytest.mark.asyncio
async def test_authenticated_duplicate_key_json_is_rejected_before_handler(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=b'{"model":"codex","model":"other"}',
            headers={**AUTH, "Content-Type": "application/json"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["error"]["code"] == "invalid_json"
    assert "codex" not in repr(body)
    assert "other" not in repr(body)


@pytest.mark.asyncio
async def test_valid_bearer_reaches_protected_handler(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=b"{}",
            headers={**AUTH, "Content-Type": "application/json"},
        )
        body = await response.json()

    assert response.status == 400
    assert body["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_request_id_is_server_generated_fixed_size(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        first = await client.get("/healthz", headers={"X-Request-ID": "client-authority"})
        second = await client.get("/healthz", headers={"X-Request-ID": "client-authority"})

    first_id = first.headers["X-Request-ID"]
    second_id = second.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", first_id)
    assert re.fullmatch(r"[0-9a-f]{32}", second_id)
    assert first_id != second_id
    assert first_id != "client-authority"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://chatgpt.com/backend-api/codex",
        "https://evil.example/backend-api/codex",
        "https://chatgpt.com:444/backend-api/codex",
        "https://user@chatgpt.com/backend-api/codex",
        "https://chatgpt.com/backend-api/codex/",
        "https://chatgpt.com/backend-api/codex/responses",
        "https://chatgpt.com/backend-api/codex?redirect=evil",
        "https://chatgpt.com/backend-api/codex#fragment",
    ],
)
async def test_readyz_rejects_noncanonical_credential_url(
    tmp_path: Path,
    base_url: str,
) -> None:
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential(base_url=base_url)))

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")

    assert response.status == 503


@pytest.mark.asyncio
async def test_readyz_rejects_credential_inside_expiry_skew(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential(expires_at=1)))

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")

    assert response.status == 503


@pytest.mark.asyncio
async def test_readyz_accepts_fresh_credential_without_account_id(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential(account_id=None)))

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")

    assert response.status == 200


@pytest.mark.asyncio
async def test_readyz_rejects_empty_upstream_token(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), StaticCredentialManager(_credential(access_token="")))

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")

    assert response.status == 503


@pytest.mark.asyncio
async def test_readyz_contains_unexpected_provider_exception_without_log_leak(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(_settings(tmp_path), UnexpectedFailingCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")
        body = await response.json()

    assert response.status == 503
    assert body == {"status": "unavailable"}
    assert "SENSITIVE_UNEXPECTED_DETAIL" not in repr(body)
    assert "SENSITIVE_UNEXPECTED_DETAIL" not in caplog.text


@pytest.mark.asyncio
async def test_readyz_returns_sanitized_503_when_credentials_are_unavailable(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), FailingCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")
        body = await response.json()

    assert response.status == 503
    assert body == {"status": "unavailable"}
    assert "SENSITIVE_RESOLVER_DETAIL" not in repr(body)


@pytest.mark.asyncio
async def test_readyz_accepts_fresh_credential_with_canonical_url(tmp_path: Path) -> None:
    manager = StaticCredentialManager(_credential())
    app = create_app(_settings(tmp_path), manager)

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/readyz")
        body = await response.json()

    assert response.status == 200
    assert body == {"status": "ready"}
    assert manager.calls == [False]


@pytest.mark.asyncio
async def test_healthz_is_unauthed_liveness_only(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/healthz")
        body = await response.json()

    assert response.status == 200
    assert body == {"status": "ok"}


@pytest.mark.asyncio
async def test_missing_bearer_is_rejected_before_oversized_body_read(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, NeverCredentialManager())

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            data=io.BytesIO(b"x" * (settings.max_request_body_bytes + 1)),
        )

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_models_requires_auth_and_returns_stable_secret_free_capabilities(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), upstream_model="SENSITIVE_PRIVATE_MODEL")
    upstream = FakeUpstream({})
    app = create_app(settings, NeverCredentialManager(), upstream=upstream)

    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.get("/v1/models")
        authorized = await client.get("/v1/models", headers=AUTH)
        unauthorized_body = await unauthorized.json()
        authorized_body = await authorized.json()

    assert unauthorized.status == 401
    assert unauthorized_body["error"]["code"] == "invalid_api_key"
    assert re.fullmatch(r"[0-9a-f]{32}", unauthorized.headers["X-Request-ID"])
    assert authorized.status == 200
    assert re.fullmatch(r"[0-9a-f]{32}", authorized.headers["X-Request-ID"])
    assert authorized_body == {
        "object": "list",
        "data": [
            {
                "id": "codex",
                "created": 0,
                "object": "model",
                "owned_by": "codex-openai-bridge",
                "x_codex_bridge": {
                    "chat_completions": True,
                    "responses": True,
                    "function_calling": True,
                    "embeddings": False,
                },
            }
        ],
    }
    parsed = Model.model_validate(authorized_body["data"][0])
    assert parsed.id == "codex"
    assert parsed.model_extra == {"x_codex_bridge": authorized_body["data"][0]["x_codex_bridge"]}
    serialized = json.dumps(authorized_body, sort_keys=True)
    assert TOKEN not in serialized
    assert settings.upstream_model not in serialized
    assert upstream.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        (
            {
                "Content-Type": "application/json",
                "Content-Length": "2",
                "Expect": "100-continue",
            },
            401,
            "invalid_api_key",
        ),
        (
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Content-Length": "999999999",
                "Expect": "100-continue",
            },
            413,
            "request_too_large",
        ),
        (
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Content-Length": "2",
                "Expect": "100-continue",
            },
            400,
            "unsupported_endpoint",
        ),
    ],
)
async def test_embeddings_expect_is_rejected_before_body_upload_without_admission(
    tmp_path: Path,
    headers: dict[str, str],
    status: int,
    code: str,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=FakeUpstream({}))
    writer = AsyncMock()
    request = make_mocked_request(
        "POST",
        "/v1/embeddings",
        headers=headers,
        app=app,
        writer=writer,
    )

    response = await app_module._unsupported_embeddings_expect_handler(request)

    assert isinstance(response, web.Response)
    assert response.status == status
    assert isinstance(response.body, (bytes, bytearray))
    assert json.loads(response.body)["error"]["code"] == code
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])
    writer.write.assert_not_awaited()
    assert app[app_module._ADMISSION_KEY].active_count == 0

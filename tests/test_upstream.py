from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest

import codex_openai_bridge.upstream as upstream_module
from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.upstream import (
    HttpxResponsesUpstream,
    UpstreamError,
    UpstreamErrorKind,
)

CANONICAL_BASE_URL = "https://chatgpt.com/backend-api/codex"


class _StringSubclass(str):
    pass


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        failure: Exception | None = None,
        close_failure: Exception | None = None,
        wait_after_chunks: asyncio.Event | None = None,
        waiting: asyncio.Event | None = None,
    ) -> None:
        self._chunks = chunks
        self._failure = failure
        self._close_failure = close_failure
        self._wait_after_chunks = wait_after_chunks
        self._waiting = waiting
        self.close_calls = 0
        self.iteration_starts = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iteration_starts += 1
        for chunk in self._chunks:
            yield chunk
        if self._wait_after_chunks is not None:
            if self._waiting is not None:
                self._waiting.set()
            await self._wait_after_chunks.wait()
        if self._failure is not None:
            raise self._failure

    async def aclose(self) -> None:
        self.close_calls += 1
        if self._close_failure is not None:
            raise self._close_failure


def _credential(
    *,
    access_token: str = "fake-upstream-token",
    base_url: str = CANONICAL_BASE_URL,
    account_id: str | None = "fake-account-id",
    expires_at: int = 4_102_444_800,
) -> Credential:
    return Credential(
        access_token=access_token,
        base_url=base_url,
        account_id=account_id,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_rejects_expired_credential_before_http() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match=r"^upstream request failed$"):
            await upstream.create_response(_credential(expires_at=1), {})
    finally:
        await upstream.aclose()

    assert calls == 0


@pytest.mark.asyncio
async def test_rejects_noncanonical_credential_before_http() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match=r"^upstream request failed$"):
            await upstream.create_response(
                _credential(base_url="https://example.invalid/backend-api/codex"),
                {"model": "upstream-model"},
            )
    finally:
        await upstream.aclose()

    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    [
        _credential(access_token="line\nbreak"),
        _credential(access_token=""),
        _credential(access_token="tab\tbreak"),
        _credential(access_token="delete\x7fbreak"),
        _credential(account_id="line\rbreak"),
        _credential(account_id=""),
        _credential(account_id="null\x00break"),
        _credential(access_token=_StringSubclass("fake-upstream-token")),
        _credential(base_url=_StringSubclass(CANONICAL_BASE_URL)),
        _credential(account_id=_StringSubclass("fake-account-id")),
    ],
)
async def test_rejects_credential_values_that_are_not_safe_fixed_headers(
    credential: Credential,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match=r"^upstream request failed$"):
            await upstream.create_response(credential, {})
    finally:
        await upstream.aclose()

    assert calls == 0


@pytest.mark.asyncio
async def test_posts_once_with_reconstructed_headers_and_exact_payload() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "completed"})

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    payload = {"model": "upstream-model", "store": False, "stream": False}
    credential = _credential()
    try:
        result = await upstream.create_response(credential, payload)
    finally:
        await upstream.aclose()

    assert result == {"status": "completed"}
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == CANONICAL_BASE_URL + "/responses"
    assert json.loads(request.content) == payload
    assert request.headers["Authorization"] == "Bearer " + credential.access_token
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["originator"] == "codex_cli_rs"
    assert request.headers["User-Agent"].startswith("codex_cli_rs/")
    assert request.headers["ChatGPT-Account-ID"] == credential.account_id
    assert request.headers["Host"] == "chatgpt.com"
    assert "X-Request-ID" not in request.headers
    for forbidden in (
        "Cookie",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "OpenAI-Organization",
        "OpenAI-Project",
        "Proxy-Authorization",
    ):
        assert forbidden not in request.headers
    assert request.extensions["timeout"] == {
        "connect": Settings.from_env().connect_timeout_seconds,
        "read": Settings.from_env().response_header_timeout_seconds,
        "write": Settings.from_env().total_request_deadline_seconds,
        "pool": Settings.from_env().connect_timeout_seconds,
    }


@pytest.mark.asyncio
async def test_omits_account_header_when_credential_has_no_account() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        await upstream.create_response(_credential(account_id=None), {})
    finally:
        await upstream.aclose()

    assert len(requests) == 1
    assert "ChatGPT-Account-ID" not in requests[0].headers


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_error_is_sanitized() -> None:
    calls = 0
    trap_hits = 0

    async def trap_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal trap_hits
        trap_hits += 1
        writer.close()
        await writer.wait_closed()

    trap = await asyncio.start_server(trap_handler, "127.0.0.1", 0)
    socket = trap.sockets[0]
    trap_port = socket.getsockname()[1]

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            headers={"Location": f"http://127.0.0.1:{trap_port}/credential-trap"},
        )

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()
        trap.close()
        await trap.wait_closed()

    assert calls == 1
    assert trap_hits == 0
    assert caught.value.args == ("upstream request failed",)
    assert "credential-trap" not in repr(caught.value)


@pytest.mark.asyncio
async def test_non_json_success_is_a_sanitized_upstream_error() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"SENSITIVE_UPSTREAM_BODY")

    settings = replace(Settings.from_env(), total_request_deadline_seconds=1.0)
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert caught.value.args == ("upstream request failed",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "SENSITIVE_UPSTREAM_BODY" not in repr(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_total_request_deadline_bounds_the_whole_call() -> None:
    never_respond = asyncio.Event()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await never_respond.wait()
        raise AssertionError("unreachable")

    settings = replace(
        Settings.from_env(),
        connect_timeout_seconds=0.1,
        response_header_timeout_seconds=0.1,
        total_request_deadline_seconds=0.1,
    )
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match=r"^upstream request failed$"):
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers", [{"Content-Length": "17"}, {}, {"Transfer-Encoding": "chunked"}]
)
async def test_streamed_body_at_exact_limit_is_accepted(headers: dict[str, str]) -> None:
    content = b'{"status":"ok"}'
    assert len(content) == 15
    if "Content-Length" in headers:
        headers["Content-Length"] = str(len(content))
    stream = _TrackingStream([content[:4], content[4:]])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream)

    settings = replace(Settings.from_env(), max_upstream_body_bytes=len(content))
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        result = await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert result == {"status": "ok"}
    assert stream.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers", [{"Content-Length": "16"}, {}, {"Transfer-Encoding": "chunked"}]
)
async def test_streamed_body_one_over_limit_is_rejected_and_closed(
    headers: dict[str, str],
) -> None:
    content = b'{"status":"ok"}'
    limit = len(content) - 1
    if "Content-Length" in headers:
        headers["Content-Length"] = str(len(content))
    stream = _TrackingStream([content])
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers=headers, stream=stream)

    settings = replace(Settings.from_env(), max_upstream_body_bytes=limit)
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match=r"^upstream request failed$"):
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert stream.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("SENSITIVE_CONNECT_TIMEOUT"),
        httpx.ReadTimeout("SENSITIVE_HEADER_TIMEOUT"),
    ],
)
async def test_transport_timeout_is_sanitized_and_not_retried(failure: Exception) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise failure

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert caught.value.args == ("upstream request failed",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "SENSITIVE" not in repr(caught.value)


@pytest.mark.asyncio
async def test_partial_response_failure_is_closed_sanitized_and_not_retried() -> None:
    stream = _TrackingStream(
        [b'{"partial":'],
        failure=httpx.ReadError("SENSITIVE_AMBIGUOUS_DISCONNECT"),
    )
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=stream)

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert stream.close_calls == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "SENSITIVE" not in repr(caught.value)


@pytest.mark.asyncio
async def test_close_failure_does_not_replace_total_deadline() -> None:
    never_finish = asyncio.Event()
    waiting = asyncio.Event()
    stream = _TrackingStream(
        [],
        close_failure=RuntimeError("SENSITIVE_CLOSE_FAILURE"),
        wait_after_chunks=never_finish,
        waiting=waiting,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    settings = replace(
        Settings.from_env(),
        connect_timeout_seconds=0.05,
        response_header_timeout_seconds=0.05,
        total_request_deadline_seconds=0.05,
    )
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert waiting.is_set()
    assert stream.close_calls == 1
    assert caught.value.kind is UpstreamErrorKind.TIMEOUT
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_close_failure_does_not_replace_external_cancellation() -> None:
    never_finish = asyncio.Event()
    waiting = asyncio.Event()
    stream = _TrackingStream(
        [],
        close_failure=RuntimeError("SENSITIVE_CLOSE_FAILURE"),
        wait_after_chunks=never_finish,
        waiting=waiting,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(upstream.create_response(_credential(), {}))
    try:
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await upstream.aclose()

    assert stream.close_calls == 1


@pytest.mark.asyncio
async def test_external_cancellation_propagates_and_closes_response() -> None:
    never_finish = asyncio.Event()
    waiting = asyncio.Event()
    stream = _TrackingStream([], wait_after_chunks=never_finish, waiting=waiting)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=stream)

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(upstream.create_response(_credential(), {}))
    try:
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await upstream.aclose()

    assert calls == 1
    assert stream.close_calls == 1


@pytest.mark.asyncio
async def test_total_deadline_bounds_body_drain_and_closes_response() -> None:
    never_finish = asyncio.Event()
    waiting = asyncio.Event()
    stream = _TrackingStream([], wait_after_chunks=never_finish, waiting=waiting)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    settings = replace(
        Settings.from_env(),
        connect_timeout_seconds=0.05,
        response_header_timeout_seconds=0.05,
        total_request_deadline_seconds=0.05,
    )
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match=r"^upstream request failed$"):
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert waiting.is_set()
    assert stream.close_calls == 1


@pytest.mark.asyncio
async def test_total_deadline_can_interrupt_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decode_started = threading.Event()
    release_decode = threading.Event()
    real_loads = json.loads

    def blocking_loads(value: str | bytes | bytearray) -> object:
        decode_started.set()
        if not release_decode.wait(timeout=1.0):
            raise AssertionError("decode was not released")
        return real_loads(value)

    monkeypatch.setattr(json, "loads", blocking_loads)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    settings = replace(
        Settings.from_env(),
        connect_timeout_seconds=0.05,
        response_header_timeout_seconds=0.05,
        total_request_deadline_seconds=0.05,
    )
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    started_at = time.monotonic()
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        release_decode.set()
        await upstream.aclose()

    assert decode_started.is_set()
    assert time.monotonic() - started_at < 0.5
    assert caught.value.kind is UpstreamErrorKind.TIMEOUT


@pytest.mark.asyncio
async def test_total_deadline_is_checked_after_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter([100.0, 102.0])
    monkeypatch.setattr(
        upstream_module, "_monotonic", lambda: next(monotonic_values), raising=False
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    settings = replace(Settings.from_env(), total_request_deadline_seconds=1.0)
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match=r"^upstream request failed$"):
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_first_401_refreshes_once_and_resends_once_without_reading_401_body() -> None:
    initial = _credential(access_token="initial-token", account_id="initial-account")
    refreshed = _credential(access_token="refreshed-token", account_id="refreshed-account")
    unauthorized_stream = _TrackingStream([b"SENSITIVE_401_BODY"])
    requests: list[httpx.Request] = []
    refresh_calls = 0

    async def refresh() -> Credential:
        nonlocal refresh_calls
        refresh_calls += 1
        return refreshed

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, stream=unauthorized_stream)
        return httpx.Response(200, json={"status": "completed"})

    upstream = HttpxResponsesUpstream(
        Settings.from_env(),
        transport=httpx.MockTransport(handler),
        credential_refresher=refresh,
    )
    try:
        result = await upstream.create_response(initial, {})
    finally:
        await upstream.aclose()

    assert result == {"status": "completed"}
    assert refresh_calls == 1
    assert len(requests) == 2
    assert requests[0].headers["Authorization"] == "Bearer initial-token"
    assert requests[0].headers["ChatGPT-Account-ID"] == "initial-account"
    assert requests[1].headers["Authorization"] == "Bearer refreshed-token"
    assert requests[1].headers["ChatGPT-Account-ID"] == "refreshed-account"
    assert unauthorized_stream.iteration_starts == 0
    assert unauthorized_stream.close_calls == 1


@pytest.mark.asyncio
async def test_second_401_is_not_retried_or_read() -> None:
    streams = [_TrackingStream([b"FIRST_SECRET"]), _TrackingStream([b"SECOND_SECRET"])]
    calls = 0
    refresh_calls = 0

    async def refresh() -> Credential:
        nonlocal refresh_calls
        refresh_calls += 1
        return _credential(access_token="refreshed-token")

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        stream = streams[calls]
        calls += 1
        return httpx.Response(401, stream=stream)

    upstream = HttpxResponsesUpstream(
        Settings.from_env(),
        transport=httpx.MockTransport(handler),
        credential_refresher=refresh,
    )
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 2
    assert refresh_calls == 1
    assert [stream.iteration_starts for stream in streams] == [0, 0]
    assert [stream.close_calls for stream in streams] == [1, 1]
    assert "SECRET" not in repr(caught.value)


@pytest.mark.asyncio
async def test_refresh_failure_is_sanitized_and_does_not_resend() -> None:
    calls = 0
    refresh_calls = 0

    async def refresh() -> Credential:
        nonlocal refresh_calls
        refresh_calls += 1
        raise RuntimeError("SENSITIVE_REFRESH_FAILURE")

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    upstream = HttpxResponsesUpstream(
        Settings.from_env(),
        transport=httpx.MockTransport(handler),
        credential_refresher=refresh,
    )
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert refresh_calls == 1
    assert caught.value.args == ("upstream request failed",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "SENSITIVE" not in repr(caught.value)


@pytest.mark.asyncio
async def test_invalid_refreshed_credential_is_rejected_before_resend() -> None:
    calls = 0
    refresh_calls = 0

    async def refresh() -> Credential:
        nonlocal refresh_calls
        refresh_calls += 1
        return _credential(base_url="http://127.0.0.1/credential-trap")

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    upstream = HttpxResponsesUpstream(
        Settings.from_env(),
        transport=httpx.MockTransport(handler),
        credential_refresher=refresh,
    )
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert refresh_calls == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "credential-trap" not in repr(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [300, 307, 400, 403, 404, 500, 502, 503, 599])
async def test_redirect_unallowlisted_4xx_and_5xx_are_generic_service_failures(
    status: int,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b"SENSITIVE_STATUS_BODY")

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert caught.value.kind is UpstreamErrorKind.SERVICE
    assert caught.value.retry_after is None
    assert caught.value.args == ("upstream request failed",)
    assert "SENSITIVE" not in repr(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", "0"),
        ("120", "120"),
        ("0007", None),
        ("", None),
        ("-1", None),
        ("86401", None),
        ("999999999999999999999999", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("SENSITIVE_RETRY_VALUE", None),
    ],
)
async def test_429_has_only_safe_bounded_retry_after(value: str, expected: str | None) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": value}, content=b"SENSITIVE_BODY")

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert caught.value.kind is UpstreamErrorKind.RATE_LIMIT
    assert caught.value.retry_after == expected
    assert value not in repr(caught.value) or value in {"", "0"}


@pytest.mark.asyncio
async def test_duplicate_retry_after_is_dropped_and_429_is_not_retried() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers=[("Retry-After", "1"), ("Retry-After", "2")],
        )

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert caught.value.kind is UpstreamErrorKind.RATE_LIMIT
    assert caught.value.retry_after is None


@pytest.mark.asyncio
async def test_timeout_and_ambiguous_transport_have_timeout_category() -> None:
    failures = [
        httpx.ConnectTimeout("CONNECT_SECRET"),
        httpx.ReadTimeout("READ_SECRET"),
        httpx.ReadError("AMBIGUOUS_SECRET"),
        httpx.RemoteProtocolError("PROTOCOL_SECRET"),
    ]
    for failure in failures:
        calls = 0

        async def handler(
            _request: httpx.Request,
            current_failure: Exception = failure,
        ) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise current_failure

        upstream = HttpxResponsesUpstream(
            Settings.from_env(), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(UpstreamError) as caught:
                await upstream.create_response(_credential(), {})
        finally:
            await upstream.aclose()

        assert calls == 1
        assert caught.value.kind is UpstreamErrorKind.TIMEOUT
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_invalid_initial_credential_and_refresh_failure_have_credential_category() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    async def failed_refresh() -> Credential:
        raise RuntimeError("REFRESH_SECRET")

    initial_failure = HttpxResponsesUpstream(
        Settings.from_env(), transport=httpx.MockTransport(handler)
    )
    refresh_failure = HttpxResponsesUpstream(
        Settings.from_env(),
        transport=httpx.MockTransport(handler),
        credential_refresher=failed_refresh,
    )
    try:
        with pytest.raises(UpstreamError) as initial_caught:
            await initial_failure.create_response(_credential(expires_at=1), {})
        with pytest.raises(UpstreamError) as refresh_caught:
            await refresh_failure.create_response(_credential(), {})
    finally:
        await initial_failure.aclose()
        await refresh_failure.aclose()

    assert calls == 1
    assert initial_caught.value.kind is UpstreamErrorKind.CREDENTIALS
    assert refresh_caught.value.kind is UpstreamErrorKind.CREDENTIALS

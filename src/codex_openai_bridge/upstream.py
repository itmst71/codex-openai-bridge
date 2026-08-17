"""Bounded non-streaming Responses HTTP capability."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum, auto
from typing import Any, Protocol

import httpx

from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.translation import (
    ResponsesStreamValidator,
    UpstreamResponseError,
    parse_responses_sse,
)

_ORIGINATOR = "codex_cli_rs"
_USER_AGENT = "codex_cli_rs/0.0.0"
_CANONICAL_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CANONICAL_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_VISIBLE_HEADER_VALUE = re.compile(r"[\x21-\x7e]+")
_DECIMAL_CONTENT_LENGTH = re.compile(r"(?:0|[1-9][0-9]*)")
_DECIMAL_RETRY_AFTER = re.compile(r"(?:0|[1-9][0-9]{0,23})")
_MAX_RETRY_AFTER_SECONDS = 86_400
_monotonic = time.monotonic


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _decode_json_body(raw: bytes) -> object:
    text = raw.decode("utf-8", errors="strict")
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
    )


def _normalized_retry_after(value: object) -> str | None:
    if type(value) is not str or _DECIMAL_RETRY_AFTER.fullmatch(value) is None:
        return None
    seconds = int(value)
    if seconds > _MAX_RETRY_AFTER_SECONDS:
        return None
    return str(seconds)


class UpstreamErrorKind(Enum):
    """Closed failure categories exposed to the application mapping layer."""

    SERVICE = auto()
    TIMEOUT = auto()
    CREDENTIALS = auto()
    RATE_LIMIT = auto()


class UpstreamError(RuntimeError):
    """Sanitized failure from the upstream capability."""

    def __init__(
        self,
        kind: UpstreamErrorKind = UpstreamErrorKind.SERVICE,
        *,
        retry_after: str | None = None,
    ) -> None:
        super().__init__("upstream request failed")
        self._kind = kind if type(kind) is UpstreamErrorKind else UpstreamErrorKind.SERVICE
        self._retry_after = (
            _normalized_retry_after(retry_after)
            if self._kind is UpstreamErrorKind.RATE_LIMIT
            else None
        )

    @property
    def kind(self) -> UpstreamErrorKind:
        return self._kind

    @property
    def retry_after(self) -> str | None:
        return self._retry_after


class _Unauthorized(Exception):
    pass


class _RefreshUnavailable(Exception):
    pass


class _CredentialsUnavailable(Exception):
    pass


class _ResponseFailure(Exception):
    def __init__(self, kind: UpstreamErrorKind, *, retry_after: str | None = None) -> None:
        self.kind = kind
        self.retry_after = retry_after


class ResponsesUpstream(Protocol):
    """Injected capability used by the application route."""

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object: ...


class ResponsesByteStream(Protocol):
    """Owned upstream byte stream; closing is explicit and idempotent."""

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class StreamingResponsesUpstream(Protocol):
    """Injected streaming capability used only for stream:true requests."""

    async def create_stream(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> ResponsesByteStream: ...


class OwnedStreamingResponsesUpstream(StreamingResponsesUpstream, Protocol):
    """Streaming transport owned by the application lifecycle."""

    async def aclose(self) -> None: ...


class BufferedResponsesUpstream:
    """Expose public non-stream calls over the stream-only Codex backend."""

    def __init__(self, upstream: OwnedStreamingResponsesUpstream, settings: Settings) -> None:
        self._upstream = upstream
        self._settings = settings

    async def create_stream(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> ResponsesByteStream:
        return await self._upstream.create_stream(credential, payload)

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        streaming_payload = dict(payload)
        streaming_payload["stream"] = True
        streaming_payload["store"] = False
        streaming_payload["include"] = ["reasoning.encrypted_content"]
        stream = await self._upstream.create_stream(credential, streaming_payload)
        validator = ResponsesStreamValidator(
            public_model=self._settings.public_model,
            max_unknown_events=self._settings.max_json_nodes,
            max_string_bytes=self._settings.max_string_bytes,
        )
        try:
            async for event in parse_responses_sse(
                stream,
                max_sse_event_bytes=self._settings.max_sse_event_bytes,
                max_stream_bytes=self._settings.max_stream_bytes,
                max_json_depth=self._settings.max_json_depth,
                max_json_nodes=self._settings.max_json_nodes,
                max_string_bytes=self._settings.max_string_bytes,
            ):
                validator.feed(event)
            response = validator.completed_response
            if response is None:
                raise UpstreamResponseError("invalid upstream response")
            return response
        finally:
            primary_error = sys.exception()
            try:
                await stream.aclose()
            except BaseException:
                if primary_error is None:
                    raise

    async def aclose(self) -> None:
        await self._upstream.aclose()


def _credential_is_valid(credential: object) -> bool:
    return (
        type(credential) is Credential
        and type(credential.access_token) is str
        and _VISIBLE_HEADER_VALUE.fullmatch(credential.access_token) is not None
        and type(credential.base_url) is str
        and credential.base_url == _CANONICAL_BASE_URL
        and (
            credential.account_id is None
            or (
                type(credential.account_id) is str
                and _VISIBLE_HEADER_VALUE.fullmatch(credential.account_id) is not None
            )
        )
        and type(credential.expires_at) is int
        and credential.expires_at > time.time()
    )


def _safe_retry_after(response: httpx.Response) -> str | None:
    values = response.headers.get_list("Retry-After")
    if len(values) != 1:
        return None
    return _normalized_retry_after(values[0])


class HttpxResponsesUpstream:
    """Bounded HTTP implementation with one credential-refresh retry on 401."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        credential_refresher: Callable[[], Awaitable[Credential]] | None = None,
    ) -> None:
        self._total_deadline = settings.total_request_deadline_seconds
        self._stream_idle_deadline = settings.stream_idle_timeout_seconds
        self._max_body_bytes = settings.max_upstream_body_bytes
        self._max_stream_bytes = settings.max_stream_bytes
        self._credential_refresher = credential_refresher
        self._timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.response_header_timeout_seconds,
            write=settings.total_request_deadline_seconds,
            pool=settings.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=self._timeout,
            transport=transport,
        )

    def _headers(self, credential: Credential, *, streaming: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {credential.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streaming else "application/json",
            "originator": _ORIGINATOR,
            "User-Agent": _USER_AGENT,
        }
        if streaming:
            headers["Accept-Encoding"] = "identity"
        if credential.account_id is not None:
            headers["ChatGPT-Account-ID"] = credential.account_id
        return headers

    def _validate_content_length(self, response: httpx.Response) -> None:
        values = response.headers.get_list("Content-Length")
        if not values:
            return
        if (
            len(values) != 1
            or _DECIMAL_CONTENT_LENGTH.fullmatch(values[0]) is None
            or int(values[0]) > self._max_body_bytes
        ):
            raise ValueError

    async def _read_response(self, response: httpx.Response) -> object:
        self._validate_content_length(response)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = self._max_body_bytes + 1 - len(body)
            body.extend(chunk[:remaining])
            if len(chunk) > remaining or len(body) > self._max_body_bytes:
                raise ValueError
        return await asyncio.to_thread(_decode_json_body, bytes(body))

    async def _request_once(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        request = self._client.build_request(
            "POST",
            _CANONICAL_RESPONSES_URL,
            headers=self._headers(credential),
            json=payload,
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code == 401:
                raise _Unauthorized
            if response.status_code == 429:
                raise _ResponseFailure(
                    UpstreamErrorKind.RATE_LIMIT,
                    retry_after=_safe_retry_after(response),
                )
            if not 200 <= response.status_code < 300:
                raise _ResponseFailure(UpstreamErrorKind.SERVICE)
            return await self._read_response(response)
        finally:
            primary_error = sys.exception()
            try:
                await response.aclose()
            except Exception:
                if primary_error is None:
                    raise

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        failure: UpstreamError | None = None
        try:
            deadline = _monotonic() + self._total_deadline
            async with asyncio.timeout(self._total_deadline):
                if not _credential_is_valid(credential):
                    raise _CredentialsUnavailable
                if type(payload) is not dict:
                    raise ValueError
                try:
                    result = await self._request_once(credential, payload)
                except _Unauthorized:
                    if self._credential_refresher is None:
                        raise
                    try:
                        refreshed = await self._credential_refresher()
                    except Exception:
                        raise _RefreshUnavailable from None
                    if not _credential_is_valid(refreshed):
                        raise _RefreshUnavailable from None
                    result = await self._request_once(refreshed, payload)
                if _monotonic() > deadline:
                    raise TimeoutError
                return result
        except _RefreshUnavailable:
            failure = UpstreamError(UpstreamErrorKind.CREDENTIALS)
        except _CredentialsUnavailable:
            failure = UpstreamError(UpstreamErrorKind.CREDENTIALS)
        except _ResponseFailure as error:
            failure = UpstreamError(error.kind, retry_after=error.retry_after)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
            failure = UpstreamError(UpstreamErrorKind.TIMEOUT)
        except Exception:
            failure = UpstreamError(UpstreamErrorKind.SERVICE)
        assert failure is not None
        raise failure

    def _validate_stream_headers(self, response: httpx.Response) -> None:
        lengths = response.headers.get_list("Content-Length")
        if lengths and (
            len(lengths) != 1
            or _DECIMAL_CONTENT_LENGTH.fullmatch(lengths[0]) is None
            or int(lengths[0]) > self._max_stream_bytes
        ):
            raise ValueError
        content_types = response.headers.get_list("Content-Type")
        if not content_types:
            return
        if len(content_types) != 1:
            raise ValueError
        pieces = [piece.strip().lower() for piece in content_types[0].split(";")]
        parameters = pieces[1:]
        if (
            pieces[0] != "text/event-stream"
            or len(parameters) > 1
            or any(
                parameter not in {"charset=utf-8", 'charset="utf-8"'} for parameter in parameters
            )
        ):
            raise ValueError

    async def _stream_request_once(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> httpx.Response:
        request = self._client.build_request(
            "POST",
            _CANONICAL_RESPONSES_URL,
            headers=self._headers(credential, streaming=True),
            json=payload,
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code == 401:
                raise _Unauthorized
            if response.status_code == 429:
                raise _ResponseFailure(
                    UpstreamErrorKind.RATE_LIMIT,
                    retry_after=_safe_retry_after(response),
                )
            if not 200 <= response.status_code < 300:
                raise _ResponseFailure(UpstreamErrorKind.SERVICE)
            self._validate_stream_headers(response)
        except BaseException:
            primary_error = sys.exception()
            try:
                await response.aclose()
            except BaseException:
                if primary_error is None:
                    raise
            raise
        return response

    async def create_stream(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> ResponsesByteStream:
        failure: UpstreamError | None = None
        response: httpx.Response | None = None
        try:
            started = _monotonic()
            deadline = started + self._total_deadline
            async with asyncio.timeout(self._total_deadline):
                if not _credential_is_valid(credential):
                    raise _CredentialsUnavailable
                if type(payload) is not dict:
                    raise ValueError
                streaming_payload = dict(payload)
                streaming_payload["stream"] = True
                streaming_payload["store"] = False
                streaming_payload["include"] = ["reasoning.encrypted_content"]
                try:
                    response = await self._stream_request_once(credential, streaming_payload)
                except _Unauthorized:
                    if self._credential_refresher is None:
                        raise
                    try:
                        refreshed = await self._credential_refresher()
                    except Exception:
                        raise _RefreshUnavailable from None
                    if not _credential_is_valid(refreshed):
                        raise _RefreshUnavailable from None
                    response = await self._stream_request_once(refreshed, streaming_payload)
                if _monotonic() > deadline:
                    raise TimeoutError
                return _HttpxResponsesByteStream(
                    response,
                    deadline=deadline,
                    idle_timeout=self._stream_idle_deadline,
                )
        except _RefreshUnavailable:
            failure = UpstreamError(UpstreamErrorKind.CREDENTIALS)
        except _CredentialsUnavailable:
            failure = UpstreamError(UpstreamErrorKind.CREDENTIALS)
        except _ResponseFailure as error:
            failure = UpstreamError(error.kind, retry_after=error.retry_after)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
            failure = UpstreamError(UpstreamErrorKind.TIMEOUT)
        except Exception:
            failure = UpstreamError(UpstreamErrorKind.SERVICE)
        if response is not None:
            try:
                await response.aclose()
            except Exception:
                pass
        assert failure is not None
        raise failure

    async def aclose(self) -> None:
        """Close the owned HTTP client."""
        await self._client.aclose()


class _HttpxResponsesByteStream:
    def __init__(self, response: httpx.Response, *, deadline: float, idle_timeout: float) -> None:
        self._response = response
        self._iterator = response.aiter_raw().__aiter__()
        self._deadline = deadline
        self._idle_timeout = idle_timeout
        self._closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def _close_suppressing(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        except Exception:
            pass

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        idle_deadline = _monotonic() + self._idle_timeout
        while True:
            remaining = min(self._deadline, idle_deadline) - _monotonic()
            if remaining <= 0:
                await self._close_suppressing()
                raise UpstreamError(UpstreamErrorKind.TIMEOUT)
            try:
                async with asyncio.timeout(remaining):
                    chunk = await anext(self._iterator)
            except StopAsyncIteration:
                await self._close_suppressing()
                raise
            except asyncio.CancelledError:
                await self._close_suppressing()
                raise
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
                await self._close_suppressing()
                raise UpstreamError(UpstreamErrorKind.TIMEOUT) from None
            except Exception:
                await self._close_suppressing()
                raise UpstreamError(UpstreamErrorKind.SERVICE) from None
            if type(chunk) is not bytes:
                await self._close_suppressing()
                raise UpstreamError(UpstreamErrorKind.SERVICE)
            if chunk:
                return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise UpstreamError(UpstreamErrorKind.SERVICE) from None

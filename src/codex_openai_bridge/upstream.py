"""Bounded non-streaming Responses HTTP capability."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable
from enum import Enum, auto
from typing import Any, Protocol

import httpx

from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings

_ORIGINATOR = "codex_cli_rs"
_USER_AGENT = "codex_cli_rs/0.0.0"
_CANONICAL_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CANONICAL_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_VISIBLE_HEADER_VALUE = re.compile(r"[\x21-\x7e]+")
_DECIMAL_CONTENT_LENGTH = re.compile(r"(?:0|[1-9][0-9]*)")
_DECIMAL_RETRY_AFTER = re.compile(r"(?:0|[1-9][0-9]{0,23})")
_MAX_RETRY_AFTER_SECONDS = 86_400
_monotonic = time.monotonic


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
        self._max_body_bytes = settings.max_upstream_body_bytes
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

    def _headers(self, credential: Credential) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {credential.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "originator": _ORIGINATOR,
            "User-Agent": _USER_AGENT,
        }
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
        return await asyncio.to_thread(json.loads, bytes(body))

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

    async def aclose(self) -> None:
        """Close the owned HTTP client."""
        await self._client.aclose()

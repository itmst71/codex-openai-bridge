from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.upstream import HttpxResponsesUpstream, UpstreamError

CANONICAL_BASE_URL = "https://chatgpt.com/backend-api/codex"


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
        _credential(account_id="line\rbreak"),
        _credential(account_id=""),
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
    assert "X-Request-ID" not in request.headers
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

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            headers={"Location": "https://example.invalid/redirect"},
        )

    upstream = HttpxResponsesUpstream(Settings.from_env(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert calls == 1
    assert caught.value.args == ("upstream request failed",)
    assert "example.invalid" not in repr(caught.value)


@pytest.mark.asyncio
async def test_non_json_success_is_a_sanitized_upstream_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"SENSITIVE_UPSTREAM_BODY")

    settings = replace(Settings.from_env(), total_request_deadline_seconds=1.0)
    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError) as caught:
            await upstream.create_response(_credential(), {})
    finally:
        await upstream.aclose()

    assert caught.value.args == ("upstream request failed",)
    assert "SENSITIVE_UPSTREAM_BODY" not in repr(caught.value)


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

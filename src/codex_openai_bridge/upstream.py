"""Bounded non-streaming Responses HTTP capability."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Protocol

import httpx

from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings

_ORIGINATOR = "codex_cli_rs"
_USER_AGENT = "codex_cli_rs/0.0.0"
_CANONICAL_BASE_URL = "https://chatgpt.com/backend-api/codex"
_VISIBLE_HEADER_VALUE = re.compile(r"[\x21-\x7e]+")


class UpstreamError(RuntimeError):
    """Sanitized failure from the upstream capability."""


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
        and _VISIBLE_HEADER_VALUE.fullmatch(credential.access_token) is not None
        and credential.base_url == _CANONICAL_BASE_URL
        and (
            credential.account_id is None
            or _VISIBLE_HEADER_VALUE.fullmatch(credential.account_id) is not None
        )
        and type(credential.expires_at) is int
        and credential.expires_at > time.time()
    )


class HttpxResponsesUpstream:
    """One-shot HTTP implementation for Task-6 Responses calls."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._total_deadline = settings.total_request_deadline_seconds
        self._timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.response_header_timeout_seconds,
            write=settings.total_request_deadline_seconds,
            pool=settings.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        try:
            if not _credential_is_valid(credential) or type(payload) is not dict:
                raise ValueError
            headers = {
                "Authorization": f"Bearer {credential.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "originator": _ORIGINATOR,
                "User-Agent": _USER_AGENT,
            }
            if credential.account_id is not None:
                headers["ChatGPT-Account-ID"] = credential.account_id
            async with asyncio.timeout(self._total_deadline):
                response = await self._client.post(
                    credential.base_url + "/responses",
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                )
                response.raise_for_status()
                return response.json()
        except Exception:
            raise UpstreamError("upstream request failed") from None

    async def aclose(self) -> None:
        """Close the owned HTTP client."""
        await self._client.aclose()

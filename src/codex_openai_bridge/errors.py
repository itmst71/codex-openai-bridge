"""OpenAI-compatible error responses."""

from __future__ import annotations

from aiohttp import web


def openai_error_response(
    *,
    status: int,
    message: str,
    error_type: str,
    code: str,
    headers: dict[str, str] | None = None,
) -> web.Response:
    """Return one bounded OpenAI-shaped error envelope."""
    return web.json_response(
        {
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
        status=status,
        headers=headers,
    )

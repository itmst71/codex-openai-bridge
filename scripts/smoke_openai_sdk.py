#!/usr/bin/env python3
"""Opt-in OpenAI SDK smoke test for an explicitly supplied bridge endpoint."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from openai import AsyncOpenAI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CODEX_BRIDGE_BASE_URL"),
        help="OpenAI API root including /v1 (or set CODEX_BRIDGE_BASE_URL)",
    )
    parser.add_argument(
        "--token-env",
        default="CODEX_BRIDGE_CLIENT_TOKEN",
        help="name of the environment variable containing the bridge token",
    )
    parser.add_argument("--model", default="codex", help="public bridge model alias")
    return parser


async def _smoke(*, base_url: str, token: str, model: str) -> None:
    async with AsyncOpenAI(
        api_key=token,
        base_url=base_url,
        max_retries=0,
        timeout=30.0,
    ) as client:
        models = await client.models.list()
        model_ids = [item.id for item in models.data]
        if model not in model_ids:
            raise RuntimeError(f"public model {model!r} was not listed")

        chat = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: bridge smoke ok"}],
        )
        chat_text = chat.choices[0].message.content
        if not chat_text:
            raise RuntimeError("chat completion returned no text")

        response = await client.responses.create(
            model=model,
            input="Reply with exactly: responses smoke ok",
        )
        if not response.output_text:
            raise RuntimeError("Responses API returned no text")

    print(f"models.list: {model_ids}")
    print(f"chat.completions.create: {chat_text}")
    print(f"responses.create: {response.output_text}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error(
            "--base-url or CODEX_BRIDGE_BASE_URL is required; no network is used by default"
        )
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"token environment variable {args.token_env!r} is not set")
    asyncio.run(_smoke(base_url=args.base_url, token=token, model=args.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

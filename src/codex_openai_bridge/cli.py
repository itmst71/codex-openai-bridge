"""Command-line entry point for the bridge service."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from aiohttp import web

from codex_openai_bridge.app import create_app
from codex_openai_bridge.auth import CredentialManager
from codex_openai_bridge.config import Settings


def main(argv: Sequence[str] | None = None) -> int:
    """Load validated settings and run the loopback aiohttp service."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        raise SystemExit("codex-openai-bridge does not accept arguments")
    settings = Settings.from_env()
    app = create_app(settings, CredentialManager(settings))
    web.run_app(app, host=settings.host, port=settings.port)
    return 0

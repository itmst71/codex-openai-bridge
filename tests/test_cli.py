from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from aiohttp import web

import codex_openai_bridge.cli as cli_module

TOKEN = "a" * 43


def test_python_module_entrypoint_uses_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "main", lambda: 7)

    with pytest.raises(SystemExit) as caught:
        runpy.run_module("codex_openai_bridge.__main__", run_name="__main__")

    assert caught.value.code == 7


def test_cli_runs_validated_application_without_touching_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "client-token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("CODEX_BRIDGE_CLIENT_TOKEN_FILE", str(token_file))
    calls: list[tuple[web.Application, dict[str, object]]] = []
    configured: list[bool] = []

    def fake_run_app(app: web.Application, **kwargs: object) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(web, "run_app", fake_run_app)
    monkeypatch.setattr(cli_module, "configure_logging", lambda: configured.append(True))

    assert cli_module.main([]) == 0
    assert len(calls) == 1
    assert configured == [True]
    app, kwargs = calls[0]
    assert isinstance(app, web.Application)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8646
    assert kwargs["access_log"] is None

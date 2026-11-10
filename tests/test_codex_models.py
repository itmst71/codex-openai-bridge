"""Official catalog discovery has bounded pipes, pagination, account binding and teardown."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codex_openai_bridge.codex_cli_models import discover_models, parse_catalog_output
from codex_openai_bridge.config import Settings
from codex_openai_bridge.model_catalog import CatalogUnavailable
from tests.test_codex_cli_auth import _document, _write_auth

_CLI = r"""
import json, os, subprocess, sys, time
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
scenario = (home / "scenario").read_text()
(home / "cli-pid").write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
    elif method == "model/list":
        if scenario == "slow":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            (home / "child-pid").write_text(str(child.pid))
            time.sleep(30)
        if scenario == "stderr":
            sys.stderr.write("SYNTHETIC_PRIVATE_DIAGNOSTIC" * 20000)
            sys.stderr.flush()
        if scenario == "stdout":
            sys.stdout.write("x" * (3 * 1024 * 1024))
            sys.stdout.flush()
        if scenario == "account_change":
            (home / "auth.json").write_text((home / "next-auth").read_text())
        page = request["params"].get("cursor")
        row = {"model": "gpt-a" if page is None else "gpt-b",
               "isDefault": page is None, "hidden": False}
        cursor = None if page is not None else "page-two"
        if scenario == "repeat":
            cursor = "page-two"
        if scenario == "missing_cursor":
            result = {"data": [row]}
        elif scenario == "duplicate":
            result = {"data": [row, row], "nextCursor": None}
        else:
            result = {"data": [row], "nextCursor": cursor}
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
"""


def _settings(tmp_path: Path, scenario: str) -> Settings:
    home = tmp_path / "cli-home"
    _write_auth(home)
    (home / "scenario").write_text(scenario)
    (home / "next-auth").write_text(json.dumps(_document("acct-other")))
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + _CLI)
    executable.chmod(0o700)
    return replace(
        Settings.from_env(),
        codex_path=executable,
        codex_home=home,
        credential_python_path=Path(sys.executable),
        helper_deadline_seconds=2.0,
    )


def _running(pid: int) -> bool:
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().split(") ", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _assert_stopped(home: Path) -> None:
    for name in ("cli-pid", "child-pid"):
        if (home / name).exists():
            assert not _running(int((home / name).read_text()))


@pytest.mark.asyncio
async def test_official_helper_collects_every_page_and_cleans_up(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "normal")
    rows = await discover_models(settings, account_id="acct-synthetic")
    assert [(row.model, row.is_default, row.hidden) for row in rows] == [
        ("gpt-a", True, False),
        ("gpt-b", False, False),
    ]
    _assert_stopped(settings.codex_home)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["repeat", "missing_cursor", "duplicate", "stderr", "stdout", "account_change", "slow"],
)
async def test_bad_discovery_is_closed_and_secret_negative(tmp_path: Path, scenario: str) -> None:
    settings = _settings(tmp_path, scenario)
    with pytest.raises(CatalogUnavailable, match=r"^model catalog is unavailable$"):
        await discover_models(settings, account_id="acct-synthetic")
    _assert_stopped(settings.codex_home)


@pytest.mark.asyncio
async def test_discovery_cancellation_joins_the_process_group(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "slow")
    pending = asyncio.create_task(discover_models(settings, account_id="acct-synthetic"))
    async with asyncio.timeout(2):
        while not (settings.codex_home / "child-pid").exists():
            await asyncio.sleep(0.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    _assert_stopped(settings.codex_home)


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_owned_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codex_openai_bridge.codex_cli_models as models_module

    settings = _settings(tmp_path, "slow")
    entered = asyncio.Event()
    release = asyncio.Event()
    original = models_module._stop_group
    processes: list[asyncio.subprocess.Process] = []

    async def delayed(process: asyncio.subprocess.Process) -> None:
        processes.append(process)
        entered.set()
        await release.wait()
        await original(process)

    monkeypatch.setattr(models_module, "_stop_group", delayed)
    pending = asyncio.create_task(discover_models(settings, account_id="acct-synthetic"))
    try:
        async with asyncio.timeout(2):
            while not (settings.codex_home / "child-pid").exists():
                await asyncio.sleep(0.01)
        pending.cancel()
        await entered.wait()
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        _assert_stopped(settings.codex_home)
    finally:
        release.set()
        for process in processes:
            await original(process)


@pytest.mark.parametrize(
    "fault", ["account", "version", "extra", "row_extra", "default", "utf8", "overflow"]
)
def test_helper_protocol_is_closed(fault: str) -> None:
    document: dict[str, Any] = {
        "version": 1,
        "account": hashlib.sha256(b"acct").hexdigest(),
        "models": [
            {"model": "gpt-a", "is_default": True, "hidden": False},
        ],
    }
    if fault == "account":
        document["account"] = "wrong"
    elif fault == "version":
        document["version"] = True
    elif fault == "extra":
        document["extra"] = "private"
    elif fault == "row_extra":
        document["models"][0]["extra"] = "private"
    elif fault == "default":
        document["models"][0]["is_default"] = False
    wire = json.dumps(document).encode()
    if fault == "utf8":
        wire = b"\xff"
    elif fault == "overflow":
        wire = b"x" * 65537
    with pytest.raises(CatalogUnavailable, match=r"^model catalog is unavailable$"):
        parse_catalog_output(wire, account_id="acct")

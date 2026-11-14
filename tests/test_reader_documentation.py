"""Reader-first docs, executable examples, and safe initial provisioning."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ("setup", "api", "models", "operations", "service-terms", "integrations")
CONSUMERS = (
    "openai-python",
    "unhush",
    "honcho",
    "langchain",
    "agents",
    "autogen",
    "aider",
    "cline",
    "continue",
)


def document_pairs() -> list[tuple[Path, Path]]:
    return (
        [(ROOT / "README.md", ROOT / "README.ja.md")]
        + [(ROOT / "docs" / f"{name}.md", ROOT / "docs" / f"{name}.ja.md") for name in GUIDES]
        + [
            (
                ROOT / "docs/integrations" / f"{name}.md",
                ROOT / "docs/integrations" / f"{name}.ja.md",
            )
            for name in CONSUMERS
        ]
    )


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    return re.findall(r"^```([^\n]*)\n(.*?)^```", text, re.MULTILINE | re.DOTALL)


def provisioning_code() -> str:
    text = (ROOT / "docs/setup.md").read_text()
    return text.split("<!-- initial-credentials -->\n```python\n", 1)[1].split("\n```", 1)[0]


def run_provisioning(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", provisioning_code()],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_readme_has_short_first_generation_path_before_reference_material() -> None:
    for path in (ROOT / "README.md", ROOT / "README.ja.md"):
        text = path.read_text()
        assert len(text.splitlines()) <= 230
        assert '"/v1/chat/completions"' not in text  # use the complete loopback URL
        assert "http://127.0.0.1:8646/v1/chat/completions" in text
        assert text.index("/v1/chat/completions") < text.index("docs/integrations")
        assert "CODEX_BRIDGE_CODEX_PATH" in text
        assert "CODEX_BRIDGE_MODEL_DISCOVERY=codex" in text
        for alias in ("codex", "default", "standard", "advanced", "fast"):
            assert f"`{alias}`" in text
        assert "client-token" in text and "continuation-key" in text
        assert "CONTRIBUTING" in text and "SECURITY.md" in text


def test_bilingual_guides_preserve_structure_and_executable_blocks() -> None:
    for english, japanese in document_pairs():
        en, ja = english.read_text(), japanese.read_text()
        assert "canonical" in en and "canonical" in ja
        assert re.findall(r"^#{1,4} ", en, re.MULTILINE) == re.findall(
            r"^#{1,4} ", ja, re.MULTILINE
        ), english
        assert [item for item in fenced_blocks(en) if item[0] != "mermaid"] == [
            item for item in fenced_blocks(ja) if item[0] != "mermaid"
        ], english
        for text in (en, ja):
            for target in re.findall(r"\]\(([^)]+)\)", text):
                if re.match(r"https?://|#", target):
                    continue
                owner = english if text == en else japanese
                relative = target.split("#", 1)[0]
                assert (owner.parent / relative).is_file(), (owner, target)


def test_python_consumer_examples_load_their_own_token() -> None:
    for name in ("langchain", "agents", "autogen", "openai-python"):
        text = (ROOT / "docs/integrations" / f"{name}.md").read_text()
        examples = [code for language, code in fenced_blocks(text) if language == "python"]
        assert examples
        for code in examples:
            tree = ast.parse(code)
            loaded = {
                n.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            stored = {
                n.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            }
            assert "bridge_token" not in loaded - stored
        assert "client-token" in text


def test_initial_provisioning_is_private_distinct_and_repeat_safe(tmp_path: Path) -> None:
    result = run_provisioning(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    root = tmp_path / ".config/codex-openai-bridge"
    paths = [root / name for name in ("client-token", "continuation-key")]
    before = [(path.read_bytes(), path.stat().st_ino) for path in paths]
    assert before[0][0] != before[1][0]
    for path in paths:
        assert re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", path.read_bytes())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    repeated = run_provisioning(tmp_path)
    assert repeated.returncode != 0
    assert repeated.stdout == ""
    assert [(path.read_bytes(), path.stat().st_ino) for path in paths] == before


@pytest.mark.parametrize("source", ["README.md", "docs/setup.md"])
def test_first_install_under_group_umask_loads_settings(tmp_path: Path, source: str) -> None:
    home = tmp_path / "fresh-home"
    home.mkdir(mode=0o700)
    if source == "README.md":
        block = next(
            code
            for language, code in fenced_blocks((ROOT / source).read_text())
            if language == "bash" and "secrets.token_urlsafe" in code
        )
        code = block.split("uv run python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    else:
        code = provisioning_code()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os; os.umask(0o002)\n"
            + code
            + "\nfrom codex_openai_bridge.config import Settings\nSettings.from_env()\n",
        ],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (stat.S_IMODE((home / ".config").stat().st_mode) & 0o022)


@pytest.mark.parametrize("existing", ["client-token", "continuation-key", "symlink"])
def test_initial_provisioning_preserves_existing_or_symlinked_entries(
    tmp_path: Path, existing: str
) -> None:
    root = tmp_path / ".config/codex-openai-bridge"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    if existing == "symlink":
        outside.write_text("preserve")
        (root / "client-token").symlink_to(outside)
    else:
        (root / existing).write_text("preserve")
    result = run_provisioning(tmp_path)
    assert result.returncode != 0
    assert result.stdout == ""
    assert len(list(root.iterdir())) == 1
    if existing == "symlink":
        assert (root / "client-token").is_symlink()
        assert outside.read_text() == "preserve"
    else:
        assert (root / existing).read_text() == "preserve"


def test_operations_separates_normal_upgrade_from_legacy_key_migration() -> None:
    text = (ROOT / "docs/operations.md").read_text()
    normal, legacy = text.split("## Legacy signing-authority migration", 1)
    assert "Preserve both existing keys" in normal
    assert "Generate and verify `continuation-key`" not in normal
    assert "cobr_r2_" in legacy and "only" in legacy
    assert "1000 <= N <= 1000000" in (ROOT / "docs/api.md").read_text()
    assert "1000 <= N <= 1000000" in (ROOT / "docs/api.ja.md").read_text()


def test_initial_provisioning_rejects_symlinked_credential_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    config = tmp_path / ".config"
    config.mkdir()
    (config / "codex-openai-bridge").symlink_to(outside, target_is_directory=True)
    before = outside.stat().st_mode
    result = run_provisioning(tmp_path)
    assert result.returncode != 0
    assert not list(outside.iterdir())
    assert outside.stat().st_mode == before


def test_concurrent_initial_provisioning_has_one_winner(tmp_path: Path) -> None:
    children = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", provisioning_code()],
            env={**os.environ, "HOME": str(tmp_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for child in children:
        stdout, _ = child.communicate(timeout=10)
        assert stdout == ""
    assert sum(child.returncode == 0 for child in children) == 1
    directory = tmp_path / ".config/codex-openai-bridge"
    values = [(directory / name).read_bytes() for name in ("client-token", "continuation-key")]
    assert values[0] != values[1]
    assert all(re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", value) for value in values)


@pytest.mark.asyncio
async def test_documented_curl_and_sdk_examples_execute_against_strict_bridge() -> None:
    from dataclasses import replace
    from types import MappingProxyType

    from aiohttp.test_utils import TestServer

    from codex_openai_bridge.app import create_app
    from codex_openai_bridge.config import Settings
    from tests.contract._support import (
        RecordingUpstream,
        StaticCredentialProvider,
        completed_text_response,
    )

    home = Path(os.environ["HOME"])
    initialized = run_provisioning(home)
    assert initialized.returncode == 0, initialized.stderr
    settings = replace(
        Settings.from_env(),
        model_map=MappingProxyType(
            {"codex": "server-owned-model", "default": "server-owned-model"}
        ),
    )
    upstream = RecordingUpstream(
        responses=[completed_text_response("docs curl"), completed_text_response("docs sdk")]
    )
    server = TestServer(create_app(settings, StaticCredentialProvider(), upstream=upstream))
    await server.start_server()
    try:
        curl = next(
            code
            for language, code in fenced_blocks((ROOT / "README.md").read_text())
            if language == "bash" and "--data" in code
        )
        curl = curl.replace("http://127.0.0.1:8646", str(server.make_url("/")).rstrip("/"))
        result = await asyncio.to_thread(
            subprocess.run,
            ["bash", "-c", curl],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["choices"][0]["message"]["content"] == "docs curl"
        sdk = next(
            code
            for language, code in fenced_blocks(
                (ROOT / "docs/integrations/openai-python.md").read_text()
            )
            if language == "python"
        )
        sdk = sdk.replace("http://127.0.0.1:8646/v1", str(server.make_url("/v1")))
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-I", "-c", sdk],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={**os.environ, "NO_PROXY": "*"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "docs sdk"
        assert len(upstream.calls) == 2
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("japanese", [False, True])
async def test_sdk_example_runs_when_discovery_has_no_codex_alias(japanese: bool) -> None:
    from dataclasses import replace
    from types import MappingProxyType

    from aiohttp.test_utils import TestServer

    from codex_openai_bridge.app import create_app
    from codex_openai_bridge.config import Settings
    from codex_openai_bridge.model_catalog import CatalogModel
    from tests.contract._support import (
        RecordingUpstream,
        StaticCredentialProvider,
        completed_text_response,
    )

    initialized = run_provisioning(Path(os.environ["HOME"]))
    assert initialized.returncode == 0, initialized.stderr
    settings = replace(
        Settings.from_env(),
        model_discovery="codex",
        model_map=MappingProxyType({"codex": "gpt-retired"}),
    )

    async def catalog() -> list[CatalogModel]:
        return [CatalogModel("gpt-6.1-sol", True)]

    upstream = RecordingUpstream(responses=[completed_text_response("selected alias works")])
    server = TestServer(
        create_app(settings, StaticCredentialProvider(), upstream=upstream, catalog_loader=catalog)
    )
    await server.start_server()
    try:
        suffix = ".ja.md" if japanese else ".md"
        sdk = next(
            code
            for language, code in fenced_blocks(
                (ROOT / "docs/integrations" / f"openai-python{suffix}").read_text()
            )
            if language == "python"
        )
        sdk = sdk.replace('model="codex"', 'model="default"')
        sdk = sdk.replace('selected_model = "codex"', 'selected_model = "default"')
        sdk = sdk.replace("http://127.0.0.1:8646/v1", str(server.make_url("/v1")))
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-I", "-c", sdk],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={**os.environ, "NO_PROXY": "*"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "selected alias works"
        assert len(upstream.calls) == 1
        assert upstream.calls[0]["model"] == "gpt-6.1-sol"
    finally:
        await server.close()

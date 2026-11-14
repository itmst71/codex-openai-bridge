"""Publication boundaries for the user-reported Unhush formatting integration."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from codex_openai_bridge.app import create_app
from codex_openai_bridge.config import Settings
from codex_openai_bridge.model_catalog import CatalogModel
from tests.contract._support import (
    CLIENT_TOKEN,
    RecordingUpstream,
    StaticCredentialProvider,
    completed_text_response,
)

ROOT = Path(__file__).resolve().parents[1]


def test_unhush_documentation_is_scoped_attributed_and_linked() -> None:
    for suffix in (".md", ".ja.md"):
        readme = (ROOT / f"README{suffix}").read_text()
        matrix = (ROOT / "docs" / f"integrations{suffix}").read_text()
        assert "| Unhush Custom Formatting |" in readme
        assert "user-reported" in readme
        assert f"docs/integrations/unhush{suffix}" in readme
        assert "| Unhush Custom Formatting |" in matrix
        assert f"integrations/unhush{suffix}" in matrix
        text = (ROOT / "docs/integrations" / f"unhush{suffix}").read_text()
        for required in (
            "user-reported",
            "3.1.0",
            "http://127.0.0.1:8646",
            "llm_provider",
            "llm_custom_url",
            "llm_custom_key",
            "llm_model_custom",
            "llm_custom_start_cmd",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/audio/transcriptions",
            "client-token",
            "continuation-key",
            "max_tokens",
            "90",
        ):
            assert required in text
        if suffix == ".md":
            assert "without `/v1`" in text
            assert "transcript text leaves the host" in text
            assert "not a speech-to-text backend" in text
            assert "not independently reverified" in text
        else:
            assert "`/v1`を付けない" in text
            assert "テキストはホストの外へ送信" in text
            assert "音声認識バックエンドではありません" in text
            assert "独立した実運用再検証ではありません" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".md", ".ja.md"])
@pytest.mark.parametrize("max_tokens", [1, 256])
async def test_documented_unhush_chat_shape_passes_strict_bridge(
    tmp_path: Path, suffix: str, max_tokens: int
) -> None:
    text = (ROOT / "docs/integrations" / f"unhush{suffix}").read_text()
    payload = json.loads(re.findall(r"^```json\n(.*?)^```", text, re.M | re.S)[0])
    assert payload["model"] == "default"
    assert payload["max_tokens"] == 256
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    assert "<transcript>" in payload["messages"][1]["content"]
    payload["max_tokens"] = max_tokens
    token = tmp_path / "client-token"
    token.write_text(CLIENT_TOKEN + "\n")
    token.chmod(0o600)
    signing_key = tmp_path / "continuation-key"
    signing_key.write_text("d" * 43 + "\n")
    signing_key.chmod(0o600)
    settings = replace(
        Settings.from_env(),
        client_token_file=token,
        continuation_key_file=signing_key,
        model_discovery="codex",
        model_map=MappingProxyType({"codex": "gpt-retired"}),
    )

    async def catalog() -> list[CatalogModel]:
        return [CatalogModel("gpt-6.1-sol", True)]

    upstream = RecordingUpstream(responses=[completed_text_response("Clean transcript.")])
    server = TestServer(
        create_app(settings, StaticCredentialProvider(), upstream=upstream, catalog_loader=catalog)
    )
    await server.start_server()
    try:
        async with ClientSession(headers={"Authorization": f"Bearer {CLIENT_TOKEN}"}) as client:
            models = await client.get(server.make_url("/v1/models"))
            assert models.status == 200
            assert "default" in [row["id"] for row in (await models.json())["data"]]
            response = await client.post(server.make_url("/v1/chat/completions"), json=payload)
            assert response.status == 200
            result = await response.json()
        assert result["choices"][0]["message"]["content"] == "Clean transcript."
        assert len(upstream.calls) == 1
        assert upstream.calls[0]["model"] == "gpt-6.1-sol"
        assert "max_tokens" not in upstream.calls[0]
        assert "max_output_tokens" not in upstream.calls[0]
    finally:
        await server.close()

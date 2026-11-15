"""Latest-stable claims must retain exact versions, scope and historical boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {
    "openai-python": "3.51.0",
    "unhush": "3.3.4",
    "honcho": "3.6.1",
    "langchain": "1.8.0",
    "agents": "0.24.0",
    "autogen": "0.7.5",
    "aider": "0.86.2",
    "cline": "3.0.74",
    "continue-core": "1.1.0",
}


def evidence() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        (ROOT / "docs/integrations/latest-stable-evidence.json").read_text()
    )
    return value


def test_latest_stable_inventory_is_complete_unique_and_secret_negative() -> None:
    value = evidence()
    rows = value["consumers"]
    assert value["checked_on"] == "2026-11-15"
    assert value["consumer_count"] == len(rows) == len(VERSIONS)
    assert {row["id"] for row in rows} == set(VERSIONS)
    assert len({row["id"] for row in rows}) == len(rows)
    serialized = json.dumps(value)
    for forbidden in ("/home/", "/mnt/", "Bearer ", "access_token", "encrypted_content"):
        assert forbidden not in serialized
    assert value["live_trial"]["case_count"] == 16
    assert value["live_trial"]["api_request_count"] == 21
    assert value["live_trial"]["http_status_counts"] == {"200": 21}
    assert value["live_trial"]["cleanup"]["server_exit_code"] == 0
    assert value["live_trial"]["cleanup"]["server_pid_gone"] is True
    assert value["live_trial"]["cleanup"]["listener_closed"] is True


@pytest.mark.parametrize("consumer,version", VERSIONS.items())
def test_latest_stable_exact_version_and_qualification(consumer: str, version: str) -> None:
    row = next(item for item in evidence()["consumers"] if item["id"] == consumer)
    assert row["latest_stable"]["version"] == version
    assert row["latest_stable"]["source_url"].startswith("https://")
    assert row["latest_stable"]["checked_at"]
    assert row["latest_stable"]["published_at"]
    qualification = row["qualification"]
    assert qualification["level"] in {"live", "contract", "unqualified"}
    assert qualification["scope"] and qualification["not_verified"]
    assert qualification["contract_case_count"] >= 0
    assert qualification["live_case_count"] >= 0
    assert qualification["contract_case_count"] == len(qualification["contract_case_ids"])
    assert len(set(qualification["contract_case_ids"])) == qualification["contract_case_count"]
    if consumer == "honcho":
        assert qualification["level"] == "contract"
        assert qualification["contract_case_count"] == 6
        assert row["tested_tuple"]["python"] == "3.13.15"
        assert row["tested_tuple"]["bridge_python"] == "3.12.3"
        trial = row["cross_runtime_trial"]
        assert trial["separate_processes"] is True
        assert trial["http_request_count"] == trial["synthetic_upstream_call_count"] == 7
        assert trial["http_status_counts"] == {"200": 7}
        assert trial["cleanup"]["bridge_children_joined"] == 6
        assert trial["cleanup"]["remaining_listeners"] == 0
        assert trial["cleanup"]["remaining_namespace_processes"] == 0
    if qualification["level"] == "live":
        assert qualification["live_case_count"] > 0
    if qualification["level"] == "contract":
        assert qualification["contract_case_count"] > 0


@pytest.mark.parametrize(
    "suffix,latest,historical",
    [
        (".md", "## Latest stable", "## Historical baselines"),
        (".ja.md", "## 最新安定版", "## 過去の検証baseline"),
    ],
)
def test_latest_and_historical_documentation_are_separate(
    suffix: str,
    latest: str,
    historical: str,
) -> None:
    text = (ROOT / "docs" / f"integrations{suffix}").read_text()
    assert latest in text and historical in text
    section = text.split(latest, 1)[1].split(historical, 1)[0]
    assert "latest-stable-evidence.json" in section
    for version in VERSIONS.values():
        assert version in section
    assert "444897975c95393b0d48024470ece03c025d3aa4" in text.split(historical, 1)[1]
    assert "1.6.0" in text.split(historical, 1)[1]
    assert "3.1.0" in text.split(historical, 1)[1]


def test_latest_stable_ci_and_native_artifact_pins_match_evidence() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    lanes = {
        "Contract test with OpenAI SDK 3.51.0": ("openai==3.51.0",),
        "Consumer contract with LangChain OpenAI 1.8.0": (
            "langchain-openai==1.8.0",
            "langchain-core==1.8.0",
            "openai[aiohttp]==3.51.0",
            "langchain==1.5.0",
            "langgraph==1.2.22",
            "langchain-ollama==1.1.0",
            "langgraph-checkpoint-sqlite==3.1.3",
        ),
        "Consumer contract with OpenAI Agents SDK 0.24.0": (
            "openai-agents==0.24.0",
            "openai==3.51.0",
        ),
        "Consumer contract with AutoGen 0.7.5": (
            "autogen-ext[openai]==0.7.5",
            "autogen-core==0.7.5",
            "autogen-agentchat==0.7.5",
            "openai==3.51.0",
        ),
        "Consumer contract with Aider 0.86.2": (
            "aider-chat==0.86.2",
            "litellm==1.81.10",
            "openai==2.20.0",
        ),
    }
    for name, pins in lanes.items():
        block = workflow.split(f"- name: {name}\n", 1)[1].split("\n\n", 1)[0]
        for pin in pins:
            assert pin in block
    records = {row["id"]: row for row in evidence()["consumers"]}
    prefix = ROOT / "tests/consumer_contract"
    cline = json.loads((prefix / "cline_env/package.json").read_text())
    cline_lock = json.loads((prefix / "cline_env/package-lock.json").read_text())
    version = records["cline"]["latest_stable"]["version"]
    assert cline["dependencies"]["@cline/cli-linux-x64"] == version
    assert cline_lock["packages"]["node_modules/@cline/cli-linux-x64"]["version"] == version
    source = json.loads((prefix / "unhush_env/source-lock.json").read_text())
    assert source["version"] == records["unhush"]["latest_stable"]["version"]
    assert source["revision"] == records["unhush"]["latest_stable"]["revision"]
    assert source["files"] == records["unhush"]["source_files_sha256"]
    assert len(source["files"]) == 4
    continued = json.loads((prefix / "continue_env/package.json").read_text())
    assert (
        continued["dependencies"]["@continuedev/core"]
        == records["continue-core"]["latest_stable"]["version"]
    )

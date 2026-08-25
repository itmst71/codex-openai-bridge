# ruff: noqa: RUF001
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
README_JA = ROOT / "README.ja.md"
SECURITY = ROOT / "SECURITY.md"
LICENSE = ROOT / "LICENSE"


def test_publication_policy_is_explicit_and_bilingual() -> None:
    english = README.read_text(encoding="utf-8")
    japanese = README_JA.read_text(encoding="utf-8")

    for required in (
        "## Intended use and service terms",
        "personal, single-user, local experimentation",
        "Do not share the bridge client token",
        "Do not pool ChatGPT/Codex accounts",
        "team, hosted, public, commercial, or CI inference service",
        "official OpenAI API",
        "not affiliated with, endorsed by, or supported by OpenAI",
        "The MIT license applies only to this project's source code",
        "does not grant rights to access or redistribute OpenAI services",
        "https://x.com/thsottiaux/status/2090675027670978569",
        "https://openai.com/policies/row-terms-of-use/",
        "https://help.openai.com/en/articles/10471989-openai-account-sharing-policy",
    ):
        assert required in english

    for required in (
        "## 想定用途とサービス利用条件",
        "個人・単一ユーザー・ローカルでの実験用途",
        "ブリッジ client token を共有しないでください",
        "ChatGPT/Codex account をpoolしないでください",
        "team、hosted、public、commercial、CI inference service",
        "OpenAI公式API",
        "OpenAIとは提携しておらず、承認・推奨・サポートも受けていません",
        "MIT licenseはこのprojectのsource codeだけに適用されます",
        "OpenAI serviceへアクセスまたは再配布する権利を付与しません",
        "https://x.com/thsottiaux/status/2090675027670978569",
        "https://openai.com/policies/row-terms-of-use/",
        "https://help.openai.com/en/articles/10471989-openai-account-sharing-policy",
    ):
        assert required in japanese


def test_consumer_status_taxonomy_distinguishes_evidence_levels() -> None:
    english = README.read_text(encoding="utf-8")
    japanese = README_JA.read_text(encoding="utf-8")

    for required in (
        "**Contract verified**",
        "**Live verified**",
        "**Operationally verified**",
        "**Unsupported / external provider required**",
        "fixture or serializer compatibility alone is not an operational support claim",
        "Honcho | **Operationally verified**",
        "LangChain `ChatOpenAI` | **Operationally verified (scoped)**",
        "langchain-openai` 1.6.0",
        "OpenAI SDK 3.3.1",
        "one-tool and sequential multi-tool Responses",
        "asynchronous Responses streaming remains unverified",
        "reasoning may begin the next function round",
    ):
        assert required in english

    for required in (
        "**Contract verified（contract検証済み）**",
        "**Live verified（live検証済み）**",
        "**Operationally verified（実運用検証済み）**",
        "**Unsupported / external provider required（非対応／外部provider必須）**",
        "fixtureまたはserializer互換性だけでは実運用supportを意味しません",
        "Honcho | **Operationally verified（実運用検証済み）**",
        "LangChain `ChatOpenAI` | **Operationally verified（範囲限定）**",
        "langchain-openai` 1.6.0",
        "OpenAI SDK 3.3.1",
        "Responses 1-toolおよび逐次multi-tool",
        "async Responses streamingは未検証",
        "reasoningから次のfunction roundを開始できます",
    ):
        assert required in japanese


def test_security_policy_matches_personal_loopback_publication_scope() -> None:
    security = SECURITY.read_text(encoding="utf-8")

    for required in (
        "## Scope and non-goals",
        "personal, single-user, loopback-only",
        "multi-user credential distribution",
        "account pooling",
        "billing or resale",
        "public hosted deployment",
        "official OpenAI API",
        "Do not include exploit details, credentials, access tokens, account IDs",
    ):
        assert required in security


def test_mit_license_notice_does_not_claim_service_rights() -> None:
    assert "MIT License" in LICENSE.read_text(encoding="utf-8")
    english = README.read_text(encoding="utf-8")
    assert "does not grant rights to access or redistribute OpenAI services" in english

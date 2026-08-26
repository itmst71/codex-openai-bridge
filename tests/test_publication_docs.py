# ruff: noqa: RUF001
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
README_JA = ROOT / "README.ja.md"
SECURITY = ROOT / "SECURITY.md"
LICENSE = ROOT / "LICENSE"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
CONTRIBUTING_JA = ROOT / "CONTRIBUTING.ja.md"
ISSUE_TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
PULL_REQUEST_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"


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


def test_official_codex_cli_authentication_prerequisites_are_bilingual() -> None:
    english = README.read_text(encoding="utf-8")
    japanese = README_JA.read_text(encoding="utf-8")
    security = SECURITY.read_text(encoding="utf-8")

    assert english.count("## Prerequisites") == 1
    assert japanese.count("## 前提条件") == 1
    for obsolete in (
        "current release requires the Hermes-internal Codex credential resolver",
        "Official Codex CLI authentication is the intended replacement but is not supported",
    ):
        assert obsolete not in english
    for obsolete in (
        "現行releaseはHermes内部のCodex credential resolverを必要とします",
        "公式Codex CLI認証が移行先ですが、このreleaseではまだ対応していません",
    ):
        assert obsolete not in japanese

    for required in (
        "## Prerequisites",
        "official OpenAI Codex CLI",
        "codex login",
        "codex login --device-auth",
        'cli_auth_credentials_store = "file"',
        "$HOME/.codex/auth.json",
        'chmod 700 "$HOME/.codex"',
        'chmod 600 "$HOME/.codex/auth.json"',
        "Keyring and `auto` storage are not supported",
        "Codex CLI 0.146.0 is the verified version",
        "Other Codex CLI versions remain unverified",
        "Hermes is not required",
        "API-key authentication is intentionally unsupported",
        "use the official OpenAI API directly without this bridge",
    ):
        assert required in english

    for required in (
        "## 前提条件",
        "OpenAI公式Codex CLI",
        "codex login",
        "codex login --device-auth",
        'cli_auth_credentials_store = "file"',
        "$HOME/.codex/auth.json",
        'chmod 700 "$HOME/.codex"',
        'chmod 600 "$HOME/.codex/auth.json"',
        "keyringと`auto` storageは",
        "Codex CLI 0.146.0が検証済みversion",
        "他のCodex CLI versionは未検証",
        "Hermesは不要です",
        "API key認証は意図的に非対応",
        "このbridgeを使わず",
    ):
        assert required in japanese

    assert english.index('cli_auth_credentials_store = "file"') < english.index("codex login")
    assert japanese.index('cli_auth_credentials_store = "file"') < japanese.index("codex login")
    for required in (
        "Codex CLI 0.146.0 is the verified version",
        "Other Codex CLI versions remain unverified",
    ):
        assert required in security


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
        "asynchronous Responses streaming with the official aiohttp transport",
        "bounded `batch()`/`abatch()` concurrency",
        "bounded `batch()`/`abatch()` concurrency (4 inputs, maximum 2)",
        "one injected 429 and one mid-stream disconnect",
        "recovery after one injected 429 and one mid-stream disconnect",
        "openai[aiohttp]",
        "one-shot batch/concurrency evidence; repeated batch/concurrency runs remain unverified",
        "repeated exhaustion/interruption remains unverified",
        "daemon-mode use remains unverified",
        "sync_http_client = DefaultHttpxClient(trust_env=False)",
        "async_http_client = DefaultAioHttpClient(trust_env=False)",
        "http_socket_options=()",
        "http_client=sync_http_client",
        "http_async_client=async_http_client",
        "await llm.root_async_client.close()",
        "llm.root_client.close()",
        "async def main() -> None:",
        "asyncio.run(main())",
        "object=response",
        "status=completed",
        "chunk_position=last",
        "reasoning may begin the next function round",
        "langgraph` 1.2.11",
        "langchain` 1.3.17",
        "langchain-ollama` 1.1.0",
        "langgraph-checkpoint-sqlite` 3.1.1",
        "bounded LangGraph read-only graph",
        "plain-text in-memory RAG through external Ollama `embeddinggemma`",
        "standard `create_agent` in Chat Completions mode",
        "consumer-side SQLite interrupt/resume",
        "the bridge remains stateless and receives no HITL pause/resume request",
        "persistent vector stores, production-corpus retrieval evaluation",
        "PostgreSQL checkpoints, concurrent checkpoint writers, multiple interrupts",
        "standard-agent Responses mode remains unverified",
        "claims are consumer-side and scope-exact",
        "external Ollama `embeddinggemma` backend at 768 dimensions",
        "does not add Embeddings capability to the bridge",
        "standard agent has no middleware, store, or checkpointer",
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
        "公式aiohttp transportによるasync Responses streaming",
        "bounded `batch()`/`abatch()` concurrency",
        "bounded `batch()`/`abatch()` concurrency（4入力・最大2）",
        "注入した429 1回とstream途中切断1回",
        "注入した429 1回とstream途中切断1回からの回復",
        "openai[aiohttp]",
        "batch/concurrency evidenceはone-shotであり、反復batch/concurrency runは未検証",
        "反復exhaustion／interruptionは未検証",
        "daemon-mode利用は未検証",
        "sync_http_client = DefaultHttpxClient(trust_env=False)",
        "async_http_client = DefaultAioHttpClient(trust_env=False)",
        "http_socket_options=()",
        "http_client=sync_http_client",
        "http_async_client=async_http_client",
        "await llm.root_async_client.close()",
        "llm.root_client.close()",
        "async def main() -> None:",
        "asyncio.run(main())",
        "object=response",
        "status=completed",
        "chunk_position=last",
        "reasoningから次のfunction roundを開始できます",
        "langgraph` 1.2.11",
        "langchain` 1.3.17",
        "langchain-ollama` 1.1.0",
        "langgraph-checkpoint-sqlite` 3.1.1",
        "bounded LangGraph read-only graph",
        "外部Ollama `embeddinggemma`によるplain-text in-memory RAG",
        "Chat Completions modeのstandard `create_agent`",
        "consumer-side SQLite interrupt/resume",
        "bridgeはstatelessのままで、HITL pause/resume requestを受けません",
        "persistent vector store、production corpusのretrieval評価",
        "PostgreSQL checkpoint、checkpoint concurrent writer、multiple interrupt",
        "standard-agent Responses modeは未検証",
        "表記はconsumer-sideかつscope-exactです",
        "外部Ollama `embeddinggemma` backendを768 dimensionsで使用",
        "bridgeへEmbeddings capabilityを追加しません",
        "standard agentにはmiddleware、store、checkpointerがありません",
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


def test_bilingual_langchain_python_examples_are_syntactically_valid() -> None:
    for path in (README, README_JA):
        section = path.read_text(encoding="utf-8").split("### LangChain", 1)[1]
        code = section.split("```python\n", 1)[1].split("\n```", 1)[0]
        tree = ast.parse(code, filename=str(path))
        try_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
        assert len(try_nodes) == 1
        finalbody = "\n".join(ast.unparse(node) for node in try_nodes[0].finalbody)
        assert "await llm.root_async_client.close()" in finalbody
        assert "llm.root_client.close()" in finalbody


def test_mit_license_notice_does_not_claim_service_rights() -> None:
    assert "MIT License" in LICENSE.read_text(encoding="utf-8")
    english = README.read_text(encoding="utf-8")
    assert "does not grant rights to access or redistribute OpenAI services" in english


def test_contribution_acceptance_policy_and_forms_are_explicit() -> None:
    english = CONTRIBUTING.read_text(encoding="utf-8")
    japanese = CONTRIBUTING_JA.read_text(encoding="utf-8")
    normalized_english = " ".join(english.split())
    normalized_japanese = " ".join(japanese.split())
    readme = README.read_text(encoding="utf-8")
    readme_ja = README_JA.read_text(encoding="utf-8")

    assert "[日本語](CONTRIBUTING.ja.md)" in english
    assert "[English (canonical)](CONTRIBUTING.md)" in japanese
    assert "[Contributing](CONTRIBUTING.md)" in readme
    assert "[貢献ガイド](CONTRIBUTING.ja.md)" in readme_ja

    for required in (
        "Bug reports and narrowly scoped pull requests are welcome",
        "No response, review, merge, or release timeline is guaranteed",
        "Non-trivial external changes require an open issue with the `scope-approved` label",
        "comment on the issue before starting",
        (
            "Typos, broken links, and obviously incorrect documentation examples may be "
            "submitted directly"
        ),
        "A concrete consumer need",
        "Live acceptance by the Codex backend",
        "Meaningful OpenAI-compatible behavior",
        "bounded, stateless, server-owned",
        "A RED → GREEN fail-closed contract",
        "API-key authentication or fallback",
        "Do not include credentials, access tokens, account IDs, real prompts",
        "AI-assisted contributions are allowed",
        "Only report checks that you actually ran",
        "Security vulnerabilities must be reported privately",
        "Small documentation-only exceptions may omit the approved issue",
        "baseline local gates",
        "GitHub CI additionally runs",
        "Contract test with OpenAI SDK 3.1.0",
        "Consumer contract with LangChain 1.5.1",
        "Issue and pull request descriptions may be written in English or Japanese",
    ):
        assert required in normalized_english

    for required in (
        "bug reportと範囲を限定したpull requestを歓迎します",
        "応答、review、merge、release時期を保証しません",
        "non-trivialな外部貢献には`scope-approved` label付きのopen issueが必要です",
        "着手前にissueへcomment",
        "typo、broken link、明白に誤ったdocumentation exampleは直接PR可能です",
        "具体的なconsumer上の必要性",
        "Codex backendが実際に受理",
        "OpenAI互換として意味のある挙動",
        "bounded、stateless、server-owned",
        "RED → GREENのfail-closed contract",
        "API key認証またはfallback",
        "credential、access token、account ID、実prompt",
        "AI-assisted contributionを許可します",
        "実際に実行したcheckだけを報告",
        "security vulnerabilityはprivateに報告",
        "小さなdocumentation-only例外では承認済みissueを省略できます",
        "baseline local gate",
        "GitHub CIでは追加で",
        "OpenAI SDK 3.1.0 contract",
        "LangChain 1.5.1 consumer contract",
        "issueとPRのdescriptionは英語または日本語",
    ):
        assert required in normalized_japanese

    forms = {
        "bug_report.yml": (
            "Bug report",
            "Affected revision",
            "Sanitized reproduction",
            "Expected behavior",
            "Observed sanitized behavior",
            "I removed credentials, tokens, account IDs, prompts, tool arguments, reasoning data",
        ),
        "compatibility_report.yml": (
            "Consumer compatibility report",
            "Consumer and exact version",
            "Bridge endpoint and mode",
            "Generated request shape or minimal synthetic reproducer",
            "Evidence level requested",
            "This report does not claim support beyond the evidence provided",
        ),
        "feature_request.yml": (
            "Scoped feature request",
            "Concrete consumer need",
            "Codex backend evidence",
            "Proposed bounded public contract",
            "Fail-closed rejection cases",
            (
                "I understand API-key, multi-provider, multi-account, and hosted-service "
                "expansion is out of scope"
            ),
            (
                "I removed credentials, tokens, account IDs, prompts, tool arguments, "
                "reasoning data, and raw upstream responses"
            ),
        ),
        "question.yml": (
            "Usage question",
            "Question",
            "What I already checked",
            "I removed all credentials and sensitive request data",
        ),
        "security_contact.yml": (
            "Private security contact request",
            "Do not include vulnerability details",
            "Private reporting is unavailable to me",
            (
                "I included no exploit details, credentials, tokens, account IDs, prompts, "
                "tool arguments, reasoning data, or raw upstream responses"
            ),
            "Requested private contact method",
        ),
    }
    for name, required_values in forms.items():
        text = (ISSUE_TEMPLATES / name).read_text(encoding="utf-8")
        assert "Reports may be written in English or Japanese." in text
        for required in required_values:
            assert required in text

    config = (ISSUE_TEMPLATES / "config.yml").read_text(encoding="utf-8")
    assert "blank_issues_enabled: false" in config
    assert "security/advisories/new" in config
    assert "Private vulnerability report" in config
    assert (ISSUE_TEMPLATES / "security_contact.yml").exists()

    pull_request = PULL_REQUEST_TEMPLATE.read_text(encoding="utf-8")
    for required in (
        "## Linked issue",
        "Fixes #",
        "`scope-approved`",
        "## Evidence",
        "RED failure",
        "GREEN command and result",
        "I ran every check claimed above",
        "baseline local gates",
        "version-isolated CI consumer matrix",
        "No credential, token, account ID, prompt, tool argument, reasoning data",
        (
            "I did not add API-key authentication, provider fallback, multi-account, or "
            "hosted-service behavior"
        ),
        "Support wording is no stronger than the supplied contract, live, or operational evidence",
        "Descriptions and comments may be written in English or Japanese",
    ):
        assert required in pull_request

    assert CODEOWNERS.read_text(encoding="utf-8") == "* @itmst71\n"

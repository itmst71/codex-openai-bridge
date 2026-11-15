# 連携と検証範囲

[English (canonical)](integrations.md) | **日本語**

英語版を正本（canonical）とします。 [README](../README.ja.md)

Provider の機能とコンシューマー互換性は別々の契約です。ブリッジは Codex-backed API subset を狭く保ちながら、各コンシューマーが実際に送信する HTTP shape を loopback route、strict parser、upstream projection、コンシューマー固有の response parser を通してテストします。

対応状況の意味は次のとおりです。

- **Contract verified（contract検証済み）** — 固定した実packageのserializer/parserが、決定的な
  upstream fixtureに対して記載のcontractを完了しています。正確なwire shapeの証明であり、live provider利用の証明ではありません。
- **Live verified（live検証済み）** — 固定した実packageが、稼働中bridgeと実Codex backendを通して代表操作を完了しています。
- **Operationally verified（実運用検証済み）** — 実用applicationを実dataまたは実sourceで繰り返し使用し、
  記載のmulti-turn/toolおよびfailure-recovery境界まで確認しています。
- **Unsupported / external provider required（非対応／外部provider必須）** — Codex backendが能力を
  提供できないか、embedding providerなど別authorityへ意図的に分離しています。

`設定が必要`、`adapterが必要`、`範囲限定`、`役割限定`、`componentのみ`などの注記は証拠範囲を
さらに限定します。fixtureまたはserializer互換性だけでは実運用supportを意味しません。また、
どの行も記載されていないproduct surfaceのsupportを意味しません。

追加の`user-reported`は利用者の実利用報告に基づくことを示し、maintainerが独立して反復実行した証拠とは区別します。
Unhushの従来の根拠にはその注記がありました。現在のguideは、独立したrecorder fixtureを使う範囲限定の
maintainer実行による整形試験を別途記録しています。

```mermaid
flowchart LR
    contract["Contract verified<br/>実package + 決定的fixture"]
    live["Live verified<br/>実package + bridge + Codex"]
    operational["Operationally verified<br/>反復実用 + failure確認"]
    scoped["正確な範囲・version・制限"]

    contract --> live --> operational --> scoped
```

## 最新安定版 — 2026-11-15確認

公式に公開されたstable releaseを確認日で固定した結果であり、将来の最新版を自動保証するものではありません。
prerelease/dev/yankedを除外し、componentと製品CLIを区別しました。
[version・日時・公式source・実行証跡](integrations/latest-stable-evidence.json)に実際の依存tupleを記録しています。
Aiderは公開packageが固定する旧依存を維持します。

所有する隔離bridge（static `codex` → `gpt-6.1-sol`）と実Codexの試験は16項目・21API request、全HTTP200でした。
試験後にprocessとlistenerの終了を確認しています。本番反映・model discovery・製品全体の耐久試験ではありません。
今回の狭い最新版scopeと、下の旧版の実運用実績は分けて扱います。

| コンシューマー / tool | 最新安定版 | 今回の根拠 | 正確な境界 / 未検証 |
| --- | --- | --- | --- |
| OpenAI Python SDK | 3.51.0 | **Live verified** + native typed contract | Async Chat/Responsesのbuffered/stream、structured/function/custom-tool・cache-write・compaction contract。`max_retries=0`、他API・長時間挙動は未検証 |
| Unhush Custom Formatting | 3.3.4、source `657352ea22c0738fe1c3a26005c2365032d02746` | **Live verified、source clientのみ**。consumer13件・実整形3回 | 3.3.4の実packageのRecordingBar/停止/fallback/length/90秒/UI/再起動ではない。AppImageはhash確認・展開のみ。3.1.0の実package根拠は別記 |
| Honcho server | 3.6.1、source `70c67624169683b9e265517aec95a51b8077cabc` | **Contract verified、native backend/codec**。6件・HTTP7件全200 | Consumer Python3.13.15 / SDK3.51.0 / Pydantic2.14.3 → 別processの実bridge Python3.12.3 / SDK1.109.1 / Pydantic2.13.4、synthetic upstream。実provider・DB/Redis/fullserviceは未検証。#1061のnull修正は包含済み。初回3.13-bridge probeと上流回帰4件は別証拠 |
| LangChain `ChatOpenAI` | `langchain-openai`/`langchain-core` 1.8.0、SDK3.51.0 | **Live verified、範囲限定**。contract5件・live4件 | Chat/Responses text、Chat local tool往復、Responses Pydantic schema。旧RAG/LangGraph/SQLite/HITL/並列・障害試験は再実行しておらず、依存installだけでは実運用根拠にならない |
| OpenAI Agents SDK | 0.24.0、SDK3.51.0 | **Live verified、Chat model**。contract3件・live2件 | basic Runnerとstream。local tool loopはcontractのみ。tracing無効。ResponsesModel・sessions・hosted toolsは未検証 |
| AutoGen | ext/core/agentchat 0.7.5、SDK3.51.0 | **Live verified、direct client**。contract3件・live2件 | direct text/streamはlive、schemaと条件付きtool/reflection adapterはcontract。teams・hosted agents・memory/code executionは未検証 |
| Aider | 0.86.2、LiteLLM1.81.10 / SDK2.20.0 | **Contract verified**、2件。現在もlatest stable | 既存1fileの制限付きstreaming `whole`編集。公開tupleへ単体最新版依存を強制しない。live/interactive/長時間利用の追加保証ではない |
| Cline CLI Linux x64 | 3.0.74 | **Live verified**、制限付き編集。contract2件・live1件 | `--thinking none`を省略し、local-only controls・短いprompt・native guardを維持。liveは許可3種類のtoolを4回使用、HEAD不変・対象1fileのみ。他platform/roleは未検証 |
| Continue core provider | `@continuedev/core` 1.1.0、`tsx`4.23.12 | **Contract verified**、3件。現在もlatest stable | 公開component artifactそのもの。現行`cn` CLI（`@continuedev/cli`1.5.47）とは別。Chat/Edit/tool結果のみでApply/IDE/embedding/耐久試験ではない |

SDKのCI lane142件は、実SDK21件・旧Honcho request shape7件・共通usage正規化114件です。
旧shape7件を最新Honcho backendの検証とは数えません。最新native probeは別です。
追加consumerはtest-only依存で、bridge runtime依存へ追加しません。

## 過去の検証baseline

以下は当時のexact versionとscopeを保持する記録です。広いOperational評価を上の新版へ移転しません。
必要条件も当時のsnapshotに対するものです。

| コンシューマー / tool | 対応状況 | 動作確認済み version | 検証済みの範囲 | 未検証事項 / 必要条件 |
| --- | --- | --- | --- | --- |
| OpenAI Python SDK | **Live verified（live検証済み）** | 1.109.1 and 3.1.0 | live Chat/Responses non-stream/streamと、offline structured output、function/custom-tool、usage、compaction contract | `max_retries=0`を使用。非対応OpenAI API surfaceや長時間application挙動の検証ではない |
| Unhush Custom Formatting | **Operationally verified（範囲限定）** | Linux AppImage 3.1.0、sourceとpackage hashを固定 | source-lock contract、録音完了fixtureを使う隔離した実packageの整形・停止経路、実Codexのwarm-up・3回反復・注入障害からの復帰・出力長guard・実時間90秒abort・Formatting UI保存/reload・app再起動 | 整形のみ。microphone/STT、通常desktopのclipboard/typing/hotkey、UI全体、長時間運用の受入ではない。`/v1`なしURL、bridge token、cloudへのtext送信に注意。liveは`default` → `gpt-6.1-sol`。他prompt/model/build/platformを保証しない |
| Honcho | **Operationally verified（実運用検証済み）** | revision `444897975c95393b0d48024470ece03c025d3aa4`を基にしたself-hosted request shape | 反復derivation、summary/dream/dialectic生成、structured output、memory-search tool continuation、再起動、queue、既存PostgreSQL/Redis continuity | Embeddingsは別backendが必要。losslessなnullable tool-call contentは現在[plastic-labs/honcho#1061](https://github.com/plastic-labs/honcho/issues/1061)のcompatibility fixが必要 |
| LangChain `ChatOpenAI` | **Operationally verified（範囲限定）** | `langchain-openai` 1.6.0、`langchain-core` 1.6.0、`langgraph` 1.2.11、`langchain` 1.3.17、`langchain-ollama` 1.1.0、`langgraph-checkpoint-sqlite` 3.1.1、OpenAI SDK 3.3.1 | live non-stream、同期stream、公式aiohttp transportによるasync Responses streaming、strict Pydantic、Responses 1-toolおよび逐次multi-tool、Chat逐次multi-tool、3-turn history、bounded `batch()`/`abatch()` concurrency（4入力・最大2）、注入した429 1回とstream途中切断1回からの回復、bounded LangGraph read-only graph、外部Ollama `embeddinggemma`によるplain-text in-memory RAG、Chat Completions modeのstandard `create_agent`、consumer-side SQLite interrupt/resume | `temperature=None`、`max_retries=0`、`use_responses_api`選択、`openai[aiohttp]`、`trust_env=False`の明示的sync/async client、`http_socket_options=()`、typed completed terminal確認が必要。persistent vector store、production corpusのretrieval評価、PostgreSQL checkpoint、checkpoint concurrent writer、multiple interrupt、standard-agent Responses mode、daemon長期利用、2を超える並列、反復exhaustion／interruptionは未検証 |
| OpenAI Agents SDK | **Live verified（設定が必要）** | `openai-agents` 0.21.1, OpenAI SDK 3.2.0 | live `OpenAIChatCompletionsModel` basic agentとlocal `function_tool` loop。offline stream contract | tracingを無効化。`OpenAIResponsesModel`、sessions、hosted toolsは未検証 |
| AutoGen | **Contract verified（adapterが必要）** | `autogen-ext`/`autogen-core`/`autogen-agentchat` 0.7.5, OpenAI SDK 3.2.0 | Direct non-stream/stream、Pydantic JSON Schema、`AssistantAgent` local function-tool/reflection contract | explicit model metadataとconditional parallel-tools adapterが必要。live継続利用、teams、code execution、memory、hosted agentsは未検証 |
| Aider | **Contract verified（役割限定）** | `aider-chat` 0.86.2, LiteLLM 1.81.10, OpenAI SDK 2.20.0 | One-shot CLI `--message` contractで既存file 1個をstreaming `whole`-format編集 | live継続利用、interactive mode、auto-commit、repo map、architect/weak-model flows、その他edit formatsは未検証 |
| Cline CLI | **Live verified（役割限定）** | Linux x64 binary 3.0.55 | 実Codexでone-shot headless `read_files` → `editor` → `submit_and_exit`編集 | local-only flagsと短いsystem promptが必要。継続利用、他platform、default control plane、shell/web/MCP/subagents/teams、IDE/TUI、non-idempotent toolsは未検証 |
| Continue core OpenAI provider | **Contract verified（componentのみ）** | `@continuedev/core` 1.1.0, `tsx` 4.23.12 | 公開`streamChat`、Edit-role `streamComplete`、non-stream function-tool result contract | Chat/Edit componentのみ。live継続利用、Apply/file mutation、現行`cn` CLI、autocomplete、embeddings、IDE UIは未検証 |

LangChainのbatch/concurrency evidenceはone-shotであり、反復batch/concurrency runは未検証です。
反復exhaustion／interruptionは未検証であり、daemon-mode利用は未検証です。
standard-agent Responses modeは未検証です。

LangGraph、RAG、standard agent、HITLの表記はconsumer-sideかつscope-exactです。
RAGは外部Ollama `embeddinggemma` backendを768 dimensionsで使用し、bridgeへEmbeddings capabilityを追加しません。
HITLはconsumer-side SQLite checkpointerを使用し、bridgeはstatelessのままで、HITL pause/resume requestを受けません。
検証済みRAG storeはdisposable/in-memoryです。standard agentにはmiddleware、store、checkpointerがありません。
standard agentはChat Completions限定でread-only repository toolsだけを使用します。

これらの framework packages は、分離された CI contract dependencies であり、ブリッジの runtime dependencies ではありません。テストでは synthetic credentials と deterministic upstream fixtures を使用するため、通常の project tests と production deployment が frameworks の external services をインストールしたり、接続したりすることはありません。

## 連携手順

- [OpenAI Python SDK](integrations/openai-python.ja.md)
- [Unhush Custom Formatting](integrations/unhush.ja.md)
- [Honcho](integrations/honcho.ja.md)
- [LangChain](integrations/langchain.ja.md)
- [OpenAI Agents SDK](integrations/agents.ja.md)
- [AutoGen](integrations/autogen.ja.md)
- [Aider](integrations/aider.ja.md)
- [Cline CLI](integrations/cline.ja.md)
- [Continue core](integrations/continue.ja.md)

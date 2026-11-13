# Integrations and verification scope

**English (canonical)** | [日本語](integrations.ja.md)

English is canonical if translations differ. [README](../README.md)

Provider capability and consumer compatibility are separate contracts. The bridge keeps the
Codex-backed API subset narrow while testing each consumer's real emitted HTTP shape through the
loopback route, strict parser, upstream projection, and consumer-native response parser.

Status meanings:

- **Contract verified** — the pinned real package's serializer/parser completed the listed contract
  against a deterministic upstream fixture. This proves an exact wire shape, not live provider use.
- **Live verified** — the pinned real package completed a representative operation through the
  running bridge and real Codex backend.
- **Operationally verified** — a practical application was used repeatedly with real data or source,
  including the documented multi-turn/tool and failure-recovery boundaries.
- **Unsupported / external provider required** — the Codex backend cannot supply the capability, or
  the bridge intentionally leaves it to a separate authority such as an embedding provider.

Qualifiers such as `configuration required`, `adapter required`, `scoped`, `constrained role`, and
`component only` narrow those evidence levels. A fixture or serializer compatibility alone is not an operational support claim, and no row implies support for an unlisted product surface.

```mermaid
flowchart LR
    contract["Contract verified<br/>real package + deterministic fixture"]
    live["Live verified<br/>real package + bridge + Codex"]
    operational["Operationally verified<br/>practical repeated use + failures"]
    scoped["Exact scope, versions, and limitations"]

    contract --> live --> operational --> scoped
```

| Consumer / tool | Status | Known-good version | Verified scope | Not verified / required conditions |
| --- | --- | --- | --- | --- |
| OpenAI Python SDK | **Live verified** | 1.109.1 and 3.1.0 | Live Chat/Responses non-stream and stream plus offline structured output, function/custom-tool, usage, and compaction contracts | Use `max_retries=0`; this is not verification of unsupported OpenAI API surfaces or long-running application behavior |
| Honcho | **Operationally verified** | Self-hosted request shape based on revision `444897975c95393b0d48024470ece03c025d3aa4` | Repeated derivation, summary/dream/dialectic generation, structured output, memory-search tool continuation, restart, queue, and existing PostgreSQL/Redis continuity | Embeddings require a separate backend; lossless nullable tool-call content currently requires the compatibility fix tracked in [plastic-labs/honcho#1061](https://github.com/plastic-labs/honcho/issues/1061) |
| LangChain `ChatOpenAI` | **Operationally verified (scoped)** | `langchain-openai` 1.6.0, `langchain-core` 1.6.0, `langgraph` 1.2.11, `langchain` 1.3.17, `langchain-ollama` 1.1.0, `langgraph-checkpoint-sqlite` 3.1.1, OpenAI SDK 3.3.1 | Live non-stream, sync stream, asynchronous Responses streaming with the official aiohttp transport, strict Pydantic output, one-tool and sequential multi-tool Responses, sequential multi-tool Chat, three-turn history, bounded `batch()`/`abatch()` concurrency (4 inputs, maximum 2), recovery after one injected 429 and one mid-stream disconnect, a bounded LangGraph read-only graph, plain-text in-memory RAG through external Ollama `embeddinggemma`, standard `create_agent` in Chat Completions mode, and consumer-side SQLite interrupt/resume | Set `temperature=None`, `max_retries=0`, select `use_responses_api`, install `openai[aiohttp]`, own explicit sync/async clients with `trust_env=False`, set `http_socket_options=()`, and require a typed completed terminal. Persistent vector stores, production-corpus retrieval evaluation, PostgreSQL checkpoints, concurrent checkpoint writers, multiple interrupts, standard-agent Responses mode, daemon longevity, concurrency above 2, and repeated exhaustion/interruption remain unverified |
| OpenAI Agents SDK | **Live verified (configuration required)** | `openai-agents` 0.21.1, OpenAI SDK 3.2.0 | Live `OpenAIChatCompletionsModel` basic agent and local `function_tool` loop; offline stream contract | Disable tracing; `OpenAIResponsesModel`, sessions, and hosted tools are not verified |
| AutoGen | **Contract verified (adapter required)** | `autogen-ext`/`autogen-core`/`autogen-agentchat` 0.7.5, OpenAI SDK 3.2.0 | Direct non-stream/stream and Pydantic JSON Schema; one `AssistantAgent` local function-tool/reflection contract | Requires explicit model metadata and the conditional parallel-tools adapter; live sustained use, teams, code execution, memory, and hosted agents are not verified |
| Aider | **Contract verified (constrained role)** | `aider-chat` 0.86.2, LiteLLM 1.81.10, OpenAI SDK 2.20.0 | One-shot CLI `--message` contract performs a streaming `whole`-format edit of one existing file | Live sustained use, interactive mode, auto-commit, repo map, architect/weak-model flows, and other edit formats are not verified |
| Cline CLI | **Live verified (constrained role)** | Linux x64 binary 3.0.55 | One live headless `read_files` → `editor` → `submit_and_exit` edit of one existing file | Requires the documented local-only flags and short system prompt; sustained use, other platforms, default control plane, shell/web/MCP/subagents/teams, IDE/TUI, and non-idempotent tools are not verified |
| Continue core OpenAI provider | **Contract verified (component only)** | `@continuedev/core` 1.1.0, `tsx` 4.23.12 | Public `streamChat`, Edit-role `streamComplete`, and non-stream function-tool result contract | Chat/Edit component only; live sustained use, Apply/file mutation, current `cn` CLI, autocomplete, embeddings, and IDE UI are not verified |

For LangChain, this is one-shot batch/concurrency evidence; repeated batch/concurrency runs remain unverified.
In particular, repeated exhaustion/interruption remains unverified; daemon-mode use remains unverified.
The unverified scope includes persistent vector stores, production-corpus retrieval evaluation,
PostgreSQL checkpoints, concurrent checkpoint writers, and multiple interrupts;
standard-agent Responses mode remains unverified.

The LangGraph, RAG, standard-agent, and HITL claims are consumer-side and scope-exact.
RAG uses an external Ollama `embeddinggemma` backend at 768 dimensions and does not add Embeddings capability to the bridge.
HITL uses a consumer-side SQLite checkpointer; the bridge remains stateless and receives no HITL pause/resume request.
The verified RAG store is disposable/in-memory. The standard agent has no middleware, store, or checkpointer and is
Chat-Completions-only with read-only repository tools.

These framework packages are isolated CI contract dependencies, not bridge runtime dependencies.
Their tests use synthetic credentials and deterministic upstream fixtures, so normal project tests
and production deployment do not install or contact the frameworks' external services.

## Configuration recipes

- [OpenAI Python SDK](integrations/openai-python.md)
- [Honcho](integrations/honcho.md)
- [LangChain](integrations/langchain.md)
- [OpenAI Agents SDK](integrations/agents.md)
- [AutoGen](integrations/autogen.md)
- [Aider](integrations/aider.md)
- [Cline CLI](integrations/cline.md)
- [Continue core](integrations/continue.md)

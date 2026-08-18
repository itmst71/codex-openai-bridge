# codex-openai-bridge

[![CI](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/downloads/)

A bounded, loopback-only OpenAI-compatible HTTP bridge for the Codex Responses backend. It lets consumers such as the OpenAI Python SDK, Honcho, LangChain, and the OpenAI Agents SDK use a stable public model alias (`codex`) while keeping the upstream URL, OAuth authorization, account ID, and real model under server control.

This project is an independent translation service. It does **not** run the Hermes agent loop, system prompt, memory, or tools.

## Architecture

```text
OpenAI SDK / Honcho text modules
        |
        | Bearer <bridge client token>
        v
127.0.0.1:8646  codex-openai-bridge
        |  strict JSON/SSE boundaries
        |  OpenAI <-> Responses translation
        |  bounded credential helper subprocess
        v
Hermes Codex OAuth resolver -> fixed Codex Responses upstream
```

The public model name is always `codex`. The bridge reconstructs the upstream model, URL, authorization, account, `store:false`, streaming policy, and encrypted-reasoning include policy from trusted server-side state.

## Supported and unsupported

| Surface | Status |
| --- | --- |
| `GET /healthz` | Supported; process liveness only |
| `GET /readyz` | Supported; bounded credential readiness |
| `GET /v1/models` | Supported; lists only `codex` |
| `POST /v1/chat/completions` | Supported, including SSE streaming |
| `POST /v1/responses` | Supported for the live-proven stateless subset, including named Responses SSE events |
| Function tools and tool history | Supported with strict identity and call/output pairing checks |
| One named custom tool | Supported for exact declaration, call, output replay, non-stream, and stream lifecycles; the bridge never executes the tool |
| Native context compaction | Supported as an opaque bounded checkpoint lifecycle when `context_management` is explicitly requested |
| `text`, `json_object`, and `json_schema` response formats | Supported subject to upstream behavior |
| Chat `max_tokens` and `max_completion_tokens` | Accepted and strictly validated for SDK/Honcho compatibility, but not forwarded because the Codex OAuth backend rejects `max_output_tokens` |
| Direct Responses `max_output_tokens` | Unsupported and rejected rather than silently ignored |
| Reasoning, prompt cache, service tier, stream options, and text verbosity | Supported only for the exact live-proven values described below |
| Usage projection and encrypted reasoning round-trip | Supported with bounded validation; confirmed `cache_write_tokens` is retained as a typed field in SDK 3.1.0 and in `model_extra` in SDK 1.109.1, while unknown usage fields are rejected |
| `POST /v1/embeddings` | **Embeddings are not supported**; returns a sanitized unsupported error |
| `previous_response_id`, conversations, and stored response retrieval | Unsupported; the bridge remains stateless and forces `store:false` |
| Local shell, web search, computer use, MCP, hosted tools, and image input | Unsupported and rejected; no capability is emulated |
| Client-selected upstream URL/model/account/auth policy | Unsupported and ignored/rejected |
| Automatic retry of 429, 5xx, timeout, or interrupted generation | Unsupported; only one exact credential refresh/replay after upstream 401 |

The bridge does not claim OpenAI Developer API SLA equivalence, embedding support, or permanent permission to use a ChatGPT/Codex subscription as a service backend. Confirm applicable product terms and operational limits before sustained deployment.

### Direct Responses capability contract

Compatibility follows the behavior of the real Codex backend, not every field present in an
OpenAI SDK release. Unknown fields, unproven enum values, stateful features, contradictory SSE
snapshots, and malformed or expanded tool authority are rejected before they can become silent
compatibility drift.

The currently supported direct Responses request controls are:

- fixed public `model="codex"`, text `input`, and bounded user/developer/assistant message items;
- `instructions`;
- `reasoning.effort`: `none`, `low`, `medium`, `high`, `xhigh`, or `max`;
- `reasoning.summary`: `auto`, `concise`, or `detailed`;
- bounded `prompt_cache_key`;
- `service_tier="default"`;
- `stream_options={"include_obfuscation": <bool>}` on streaming requests only;
- `text.verbosity`: `low`, `medium`, or `high`, plus the documented JSON format subset;
- function tools, or exactly one custom tool with an exact named choice and
  `parallel_tool_calls=false`;
- `context_management=[{"type":"compaction","compact_threshold":N}]` with a bounded positive
  threshold; compaction cannot be combined with function or custom tools because that cross-product
  has not been proven against the backend;
- `store=false` and `include=["reasoning.encrypted_content"]`; both policies are also forced
  upstream by the server.

Developer messages use the explicit content-part form. Assistant replay items accept the
live-proven `completed`, `in_progress`, and `incomplete` status values and optional
`commentary`/`final_answer` phase. Encrypted reasoning and compaction blobs are opaque replay
authority: the bridge validates bounds, ordering, identity, and duplication without logging or
decrypting their contents.

Validated reasoning summaries are exposed only when the client explicitly requests
`reasoning.summary`; an unsolicited nonempty upstream summary is rejected. Confirmed assistant
`phase` is retained as a typed SDK 3.1.0 field and through `model_extra` in SDK 1.109.1.

Custom-tool streaming normalizes the backend's exact item/input-delta lifecycle into SDK-shaped
events, strips provider obfuscation metadata, and restores the terminal custom call when the
backend's completed snapshot omits it. Only the validated both-present or both-null identifier
variants are accepted; null identifiers are replaced with deterministic collision-checked public
identifiers. When `stream_options.include_obfuscation=false`, an omitted provider obfuscation field
is accepted while all present obfuscation values remain bounded and validated.

Offline contract tests run against the locked OpenAI Python SDK 1.109.1 and separately against
OpenAI Python SDK 3.1.0. Native compaction has a typed SDK contract only in 3.1.0; the common
Responses request, function/custom tool, non-stream, and stream subset is tested on both.

## Security assumptions

- Bind addresses must be loopback IPs. The default and deployed value is `127.0.0.1`.
- Every protected endpoint requires a separate 43-character bridge client token. This token is not the Codex OAuth token.
- The bridge client token is loaded from an owner-only, mode `0600`, non-symlink regular file.
- Codex OAuth credentials are resolved by a bounded subprocess using the Hermes Python environment. They are not copied into this repository or the systemd unit.
- Upstream URL, authorization, account ID, model, prompts, tool arguments, and encrypted reasoning are excluded from operational logs and sanitized errors.
- Request bodies, JSON structure, helper output, upstream bodies, SSE events/streams, queueing, concurrency, deadlines, and shutdown are bounded.
- `ProtectHome=read-only` is relaxed only with `ReadWritePaths=%h/.hermes` because Hermes may atomically refresh authentication state there. In systemd units, `%h` expands to the service user's home directory.
- The sandbox reduces accidental access and service compromise impact. It does not claim protection from every malicious process already running as the same Unix UID.
- Do not expose port 8646 through a public reverse proxy. If a remote trusted consumer is required, use a separately reviewed authenticated tunnel and preserve the loopback bind.

## Configuration

Create the environment and install the locked project:

```bash
git clone https://github.com/itmst71/codex-openai-bridge.git \
  "$HOME/src/codex-openai-bridge"
cd "$HOME/src/codex-openai-bridge"
uv sync --locked
```

The service reads validated environment variables. Important settings and defaults:

| Variable | Default | Notes |
| --- | --- | --- |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | Must be an IPv4 or IPv6 loopback address |
| `CODEX_BRIDGE_PORT` | `8646` | TCP port 1–65535 |
| `CODEX_BRIDGE_CLIENT_TOKEN_FILE` | `~/.config/codex-openai-bridge/client-token` | Absolute path; owner, regular file, mode `0600` |
| `CODEX_BRIDGE_UPSTREAM_MODEL` | project default | Canonical server-owned model identifier |
| `CODEX_BRIDGE_HERMES_PYTHON` | `$HOME/.hermes/hermes-agent/venv/bin/python` | Hermes credential-helper interpreter; resolved from the current user's home |
| `CODEX_BRIDGE_HELPER_PATH` | installed package helper | Absolute helper path |
| `CODEX_BRIDGE_MAX_IN_FLIGHT` | `2` | Bounded concurrent owners, maximum 10 |
| `CODEX_BRIDGE_QUEUE_WAIT_SECONDS` | `10` | Bounded admission wait |
| `CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS` | `240` | Whole request, including downstream writes |

Additional `CODEX_BRIDGE_MAX_*` and timeout variables are strictly bounded in `config.py`. Invalid, noncanonical, non-loopback, or inconsistent settings fail startup.

The checked-in user unit intentionally contains no `EnvironmentFile=` and no secret. For non-secret overrides, use a user-unit drop-in:

```ini
[Service]
Environment=CODEX_BRIDGE_PORT=8646
```

Create it with `systemctl --user edit codex-openai-bridge.service`, then run `systemctl --user daemon-reload` and restart the service.

## Generate the bridge client token

Generate exactly 32 random bytes encoded as 43 unpadded URL-safe characters. Do not print or commit the value:

```bash
install -d -m 700 "$HOME/.config/codex-openai-bridge"
umask 077
"$HOME/src/codex-openai-bridge/.venv/bin/python" -c \
  'import secrets,sys; sys.stdout.write(secrets.token_urlsafe(32) + "\n")' \
  > "$HOME/.config/codex-openai-bridge/client-token"
chmod 600 "$HOME/.config/codex-openai-bridge/client-token"
```

Keep consumer configuration pointed at the file or inject its value through that consumer's secret mechanism. Never place the value in the repository, unit, journal, or command history.

## Deploy the systemd user service

The host must provide a user manager. Logout/boot persistence additionally requires linger (`loginctl show-user "$USER" -p Linger`). Enabling linger may require an administrator.

Validate and install without starting over an occupied port:

```bash
cd "$HOME/src/codex-openai-bridge"
uv sync --locked
systemd-analyze --user verify deploy/systemd/codex-openai-bridge.service
install -Dm644 deploy/systemd/codex-openai-bridge.service \
  "$HOME/.config/systemd/user/codex-openai-bridge.service"
systemctl --user daemon-reload
systemctl --user enable codex-openai-bridge.service
```

The checked-in unit uses systemd's `%h` specifier and therefore expects this checkout at
`$HOME/src/codex-openai-bridge`. If you install it elsewhere, copy the unit and replace
`WorkingDirectory` and `ExecStart` with absolute paths for that checkout before validation.

Before starting, stop any manually launched bridge and prove port 8646 is free:

```bash
ss -ltnp '( sport = :8646 )'
systemctl --user start codex-openai-bridge.service
```

Verify the actual unit, process, listener, health, and journal:

```bash
systemctl --user is-enabled codex-openai-bridge.service
systemctl --user is-active codex-openai-bridge.service
systemctl --user show codex-openai-bridge.service \
  -p MainPID -p NRestarts -p ActiveState -p SubState
ss -ltnp '( sport = :8646 )'
curl --fail --silent --show-error http://127.0.0.1:8646/healthz
curl --fail --silent --show-error http://127.0.0.1:8646/readyz
journalctl --user -u codex-openai-bridge.service --no-pager -n 30
```

Only one authoritative bridge process should listen on the configured port.

## Honcho split configuration

Point Honcho's **text generation** modules at the bridge and keep embeddings on a distinct provider. Apply the same override to summary, dream, derivation, and dialectic model configurations that should use Codex.

```toml
[deriver.model_config]
transport = "openai"
model = "codex"

[deriver.model_config.overrides]
base_url = "http://127.0.0.1:8646/v1"
api_key_env = "CODEX_BRIDGE_CLIENT_TOKEN"

[deriver.model_config.overrides.provider_params]
structured_output_mode = "json_object"
```

Use a separate OpenAI-compatible embedding backend with its own base URL and `api_key_env`. For an initial bridge-only experiment where missing embeddings are acceptable, explicitly set:

```bash
EMBED_MESSAGES=false
```

Do not point Honcho embedding requests at this bridge.

Honcho currently sends a token-limit field on text calls. The bridge validates
that field but cannot enforce it at generation time because the ChatGPT Codex
Responses backend rejects `max_output_tokens`. The bridge still enforces its
configured total deadline, upstream/downstream byte caps, SSE-event cap, and
JSON bounds. Do not rely on a Honcho `max_tokens` value as an exact generation
limit when this bridge is the text backend.

## Verified consumer compatibility

Provider capability and consumer compatibility are separate contracts. The bridge keeps the
Codex-backed API subset narrow while testing each consumer's real emitted HTTP shape through the
loopback route, strict parser, upstream projection, and consumer-native response parser.

| Consumer | Verified version | Supported contract | Important limits |
| --- | --- | --- | --- |
| OpenAI Python SDK | 1.109.1 and 3.1.0 | Chat/Responses non-stream and stream, structured output, function/custom tools, usage; native compaction on 3.1.0 | Use `max_retries=0`; unsupported OpenAI API surfaces remain rejected |
| Honcho | request shapes from revision `444897975c95393b0d48024470ece03c025d3aa4` | text generation, structured derivation, tool loop | Embeddings require a separate backend |
| LangChain `ChatOpenAI` | `langchain-openai` 1.5.1, `langchain-core` 1.5.6, OpenAI SDK 3.2.0 | Responses non-stream/stream and Pydantic JSON Schema; Responses and Chat Completions function-tool result round-trip | Set `temperature=None`, `max_retries=0`, and choose `use_responses_api` explicitly; tool descriptions must be nonempty |
| OpenAI Agents SDK | `openai-agents` 0.21.1, OpenAI SDK 3.2.0 | `OpenAIChatCompletionsModel` text, stream, and local `function_tool` loop | Disable tracing for bridge-only use; `OpenAIResponsesModel`, sessions, and hosted tools are not claimed |
| Aider | `aider-chat` 0.86.2, LiteLLM 1.81.10, OpenAI SDK 2.20.0 | One-shot CLI `--message` performs a streaming `whole`-format edit of an existing file through Chat Completions | Use the model settings below; auto-commit, repo map, other edit formats/modes, and Aider's failure-retry behavior are not claimed |

These framework packages are isolated CI contract dependencies, not bridge runtime dependencies.
Their tests use synthetic credentials and deterministic upstream fixtures, so normal project tests
and production deployment do not install or contact the frameworks' external services.

### LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="codex",
    base_url="http://127.0.0.1:8646/v1",
    api_key=bridge_token,
    temperature=None,
    max_retries=0,
    timeout=240,
    use_responses_api=True,
)
```

Set `use_responses_api=False` to select the verified Chat Completions path instead. Pydantic models
and functions supplied through `bind_tools` need a nonempty docstring/description. A missing
description can become `description=""`, which the bridge intentionally rejects rather than
weakening its exact tool contract. Configure embeddings separately.

### OpenAI Agents SDK

```python
from agents import Agent, OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI

set_tracing_disabled(True)
client = AsyncOpenAI(
    base_url="http://127.0.0.1:8646/v1",
    api_key=bridge_token,
    max_retries=0,
    timeout=240,
)
model = OpenAIChatCompletionsModel(model="codex", openai_client=client)
agent = Agent(name="bounded-agent", instructions="Answer briefly.", model=model)
```

Tracing is disabled in the verified bridge-only configuration because tracing is a separate external
API surface. Local `function_tool` calls with nonempty docstrings are verified. The Agents SDK's
`OpenAIResponsesModel` currently emits fields outside the strict bridge Responses subset; use the
verified Chat Completions model rather than treating the entire SDK as unsupported or relaxing the
bridge parser.

### Aider

Aider uses an OpenAI-compatible Chat Completions endpoint. Register the unknown public alias with a
non-secret model settings file outside the target repository, for example at
`$HOME/.config/codex-openai-bridge/aider-model-settings.yml`, so Aider does not send an unsupported
sampling control:

```yaml
- name: openai/codex
  edit_format: whole
  weak_model_name: null
  use_repo_map: false
  use_temperature: false
  streaming: true
  cache_control: false
  caches_by_default: false
```

Aider also attempts to refresh unknown-model metadata unless it is given a local definition. Store this
second non-secret file outside the target repository, for example as
`$HOME/.config/codex-openai-bridge/aider-model-metadata.json`:

```json
{
  "openai/codex": {
    "max_tokens": 200000,
    "max_input_tokens": 200000,
    "max_output_tokens": 100000,
    "input_cost_per_token": 0,
    "output_cost_per_token": 0,
    "litellm_provider": "openai",
    "mode": "chat"
  }
}
```

The reproducibly verified role is a one-shot edit, not Aider's default interactive mode. This example
keeps Aider state outside the target repository, disables the side effects covered by the contract,
and forces LiteLLM to use its bundled model-cost map:

```bash
control_dir="$(mktemp -d)"
trap 'rm -rf -- "$control_dir"' EXIT
printf '{}\n' > "$control_dir/aider.conf.yml"
: > "$control_dir/empty.env"
IFS= read -r OPENAI_API_KEY \
  < "$HOME/.config/codex-openai-bridge/client-token"
export OPENAI_API_KEY LITELLM_LOCAL_MODEL_COST_MAP=True

aider \
  --model openai/codex \
  --openai-api-base http://127.0.0.1:8646/v1 \
  --model-settings-file "$HOME/.config/codex-openai-bridge/aider-model-settings.yml" \
  --model-metadata-file "$HOME/.config/codex-openai-bridge/aider-model-metadata.json" \
  --config "$control_dir/aider.conf.yml" \
  --env-file "$control_dir/empty.env" \
  --input-history-file "$control_dir/input.history" \
  --chat-history-file "$control_dir/chat.history.md" \
  --edit-format whole \
  --message "Set VALUE to 2." \
  --file target.py \
  --stream \
  --yes-always \
  --no-auto-commits \
  --no-dirty-commits \
  --no-gitignore \
  --no-attribute-author \
  --no-attribute-committer \
  --no-attribute-co-authored-by \
  --no-auto-lint \
  --no-auto-test \
  --no-watch-files \
  --no-cache-prompts \
  --map-tokens 0 \
  --no-analytics \
  --no-check-update \
  --no-show-release-notes \
  --no-show-model-warnings \
  --no-check-model-accepts-settings \
  --no-pretty \
  --no-fancy-input \
  --no-notifications \
  --no-detect-urls \
  --no-suggest-shell-commands \
  --no-gui \
  --no-copy-paste \
  --disable-playwright \
  --timeout 240 \
  --encoding utf-8 \
  --line-endings lf

unset OPENAI_API_KEY LITELLM_LOCAL_MODEL_COST_MAP
```

The permanent contract drives the real Aider CLI in `--message` mode, receives one streaming
whole-file listing from the production-like loopback bridge, edits exactly one tracked file, creates
no repository artifact, and leaves Git HEAD unchanged. Aider 0.86.2 contains its own bounded
transient-API retry loop, which is separate from the OpenAI SDK `max_retries=0` guidance used by
other consumers; the verified Aider role performs text editing only and has no model tool calls or
auto-commit side effect. Architect mode, weak/editor model flows, repo map, auto-commit, lint/test
repair loops, `diff`/`udiff` formats, image input, and interactive multi-turn behavior remain
unclaimed until individually tested.

The versions in this matrix are reproducible known-good baselines, not a runtime allowlist. The
bridge never receives package versions: newer consumers remain usable when their emitted HTTP
payload still satisfies the same strict contract.

## curl example

This local example reads the bridge token without printing it. A process with the same UID may still inspect process arguments, which is outside the stated same-UID threat model.

```bash
IFS= read -r bridge_token < "$HOME/.config/codex-openai-bridge/client-token"
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${bridge_token}" \
  http://127.0.0.1:8646/v1/models
unset bridge_token
```

A minimal Chat Completions request uses `model: codex`; the server replaces it with the configured upstream model.

## OpenAI SDK example

```python
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8646/v1",
    api_key=Path.home()
    .joinpath(".config/codex-openai-bridge/client-token")
    .read_text(encoding="ascii")
    .strip(),
    max_retries=0,
    timeout=240.0,
)

models = client.models.list()
assert any(model.id == "codex" for model in models.data)

completion = client.chat.completions.create(
    model="codex",
    messages=[{"role": "user", "content": "Reply briefly."}],
)
print(completion.choices[0].message.content)
```

Keep `max_retries=0`: ambiguous automatic generation retries can duplicate output or tool calls.

## Upgrade and rollback

1. Record the currently deployed Git revision without recording credentials.
2. Stop the user service and confirm its listener is gone.
3. Update the checkout to the reviewed revision.
4. Run `uv sync --locked`, the full test/type/lint gates, and `systemd-analyze --user verify`.
5. Reinstall the unit, run `systemctl --user daemon-reload`, then start and verify health/readiness/listener ownership.

```bash
cd "$HOME/src/codex-openai-bridge"
systemctl --user stop codex-openai-bridge.service
git switch --detach <reviewed-revision>
uv sync --locked
systemd-analyze --user verify deploy/systemd/codex-openai-bridge.service
install -Dm644 deploy/systemd/codex-openai-bridge.service \
  "$HOME/.config/systemd/user/codex-openai-bridge.service"
systemctl --user daemon-reload
systemctl --user start codex-openai-bridge.service
```

For rollback, repeat the same bounded procedure with the previously recorded reviewed revision and its matching lockfile/unit. Do not mix source, lockfile, virtual environment, and unit definitions from different revisions. The external token and Hermes authentication state are not part of a Git rollback.

## Opt-in live tests

Normal tests use fake loopback upstreams and no real credentials. Live tests are inert unless explicitly enabled.

With a separately started, verified bridge:

```bash
export RUN_LIVE_CODEX=1
export CODEX_BRIDGE_LIVE_BASE_URL=http://127.0.0.1:8646/v1
IFS= read -r CODEX_BRIDGE_LIVE_API_KEY \
  < "$HOME/.config/codex-openai-bridge/client-token"
export CODEX_BRIDGE_LIVE_API_KEY
uv run pytest tests/live/test_live_codex.py -q
unset RUN_LIVE_CODEX CODEX_BRIDGE_LIVE_BASE_URL CODEX_BRIDGE_LIVE_API_KEY
```

The broader SDK smoke script is also opt-in and refuses to connect without an explicit base URL and token environment variable:

```bash
export CODEX_BRIDGE_BASE_URL=http://127.0.0.1:8646/v1
IFS= read -r CODEX_BRIDGE_CLIENT_TOKEN \
  < "$HOME/.config/codex-openai-bridge/client-token"
export CODEX_BRIDGE_CLIENT_TOKEN
uv run python scripts/smoke_openai_sdk.py
unset CODEX_BRIDGE_BASE_URL CODEX_BRIDGE_CLIENT_TOKEN
```

Never include credential values in bug reports, test logs, shell transcripts, or review prompts.

## Security

Review the loopback-only threat model above before deployment. Report vulnerabilities using
the process in [SECURITY.md](SECURITY.md), and never place credentials or sensitive request
data in a public issue.

## License

Licensed under the [MIT License](LICENSE).

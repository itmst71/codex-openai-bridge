# Models and configuration reference

**English (canonical)** | [日本語](models.ja.md)

English is canonical if translations differ. [README](../README.md)

Create the environment and install the locked project:

```bash
git clone https://github.com/itmst71/codex-openai-bridge.git
cd codex-openai-bridge
uv sync --locked
```

The service reads validated environment variables. Important settings and defaults:

| Variable | Default | Notes |
| --- | --- | --- |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | Must be an IPv4 or IPv6 loopback address |
| `CODEX_BRIDGE_PORT` | `8646` | TCP port 1–65535 |
| `CODEX_BRIDGE_CLIENT_TOKEN_FILE` | `~/.config/codex-openai-bridge/client-token` | Absolute path; owner, regular file, mode `0600` |
| `CODEX_BRIDGE_CONTINUATION_KEY_FILE` | `~/.config/codex-openai-bridge/continuation-key` | Server-only continuation signing key; never give it to clients |
| `CODEX_BRIDGE_MODEL_CONFIG_FILE` | `~/.config/codex-openai-bridge/models.toml` when present | Optional absolute path to the owner-controlled alias map |
| `CODEX_BRIDGE_UPSTREAM_MODEL` | project default | Legacy single-alias model; rejected when a model map is present |
| `CODEX_BRIDGE_CODEX_PATH` | `$HOME/.local/bin/codex` | Absolute path to the official Codex CLI executable |
| `CODEX_BRIDGE_CODEX_HOME` | `$HOME/.codex` | Absolute Codex CLI home containing file-mode `auth.json` |
| `CODEX_BRIDGE_MAX_REQUEST_BODY_BYTES` | `16 MiB` | Maximum accepted request body; configurable up to 32 MiB |
| `CODEX_BRIDGE_MAX_STRING_BYTES` | `1 MiB` | Maximum UTF-8 size of one JSON string; configurable up to 2 MiB |
| `CODEX_BRIDGE_MAX_JSON_NODES` | `100,000` | Maximum validated JSON nodes; configurable up to 200,000 |
| `CODEX_BRIDGE_MAX_MESSAGES` | `1,024` | Maximum request messages/items; configurable up to 2,048 |
| `CODEX_BRIDGE_MAX_TOOLS` | `256` | Maximum request tools; configurable up to 512 |
| `CODEX_BRIDGE_MAX_JSON_DEPTH` | `32` | Maximum JSON nesting depth; configurable up to 64 |
| `CODEX_BRIDGE_MAX_UPSTREAM_BODY_BYTES` | `16 MiB` | Maximum non-streaming upstream response body; configurable up to 32 MiB |
| `CODEX_BRIDGE_MAX_SSE_EVENT_BYTES` | `1 MiB` | Maximum upstream SSE event; configurable up to 2 MiB |
| `CODEX_BRIDGE_MAX_STREAM_BYTES` | `32 MiB` | Maximum total upstream stream; configurable up to 64 MiB |
| `CODEX_BRIDGE_MAX_IN_FLIGHT` | `2` | Bounded concurrent owners, maximum 10 |
| `CODEX_BRIDGE_QUEUE_WAIT_SECONDS` | `10` | Bounded admission wait |
| `CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS` | `240` | Whole request, including downstream writes |

Additional `CODEX_BRIDGE_MAX_*` and timeout variables are strictly bounded in `config.py`. Invalid, noncanonical, non-loopback, or inconsistent settings fail startup.



## Server-owned model aliases

The following configuration is the default **static** mode.

Copy the example to enable more than the required `codex` alias:

```bash
install -d -m 700 "$HOME/.config/codex-openai-bridge"
umask 077
(set -C; cat deploy/models.toml.example > "$HOME/.config/codex-openai-bridge/models.toml")
```

```toml
version = 1

[models]
codex = "gpt-5.6-terra"
codex-sol = "gpt-5.6-sol"
```

The left side contains server-approved aliases accepted from clients. The right side contains real upstream model identifiers and remains server-owned. `codex` is mandatory, at most 16 aliases are accepted, and unknown keys, malformed identifiers, symlinks, hard links, unstable reads, or group/world-writable path authority fail startup. `CODEX_BRIDGE_MODEL_CONFIG_FILE` may select another absolute path. A map and `CODEX_BRIDGE_UPSTREAM_MODEL` are conflicting authorities and cannot be combined.

The map is loaded once and frozen. After editing it, restart the bridge; an in-flight request cannot observe a changed mapping. Changing the real model behind an existing alias intentionally invalidates prior continuation state fail-closed. `/v1/models` returns `codex` first and the remaining aliases in lexical order. Responses report the client-selected alias; real upstream model identifiers are never returned.

Use the same alias for the complete tool, reasoning, or compaction continuation. Signed public continuation IDs and opaque-state envelopes bind both the selected alias and its configured real model, and cross-alias continuation is rejected before upstream access. Finish active legacy single-alias chains before enabling a model map. To change models, finish the current chain or start a fresh chain with explicit public-text handoff.

These operator-defined aliases are not project support claims. Before assigning an alias to production work, independently live-verify that real model for every Chat, Responses, streaming, structured-output, tool, reasoning, and compaction surface that will use it.

## Automatic official catalog and default selection

To track additions, withdrawals and the official default without editing model names, opt in with
a non-secret user-unit drop-in:

```ini
[Service]
Environment=CODEX_BRIDGE_MODEL_DISCOVERY=codex
Environment=CODEX_BRIDGE_MODEL_REFRESH_SECONDS=300
Environment=CODEX_BRIDGE_MODEL_MAX_AGE_SECONDS=1800
```

After restarting once, clients may consistently request `model="default"`. The bridge obtains the
account-scoped catalog through the official CLI's `model/list`, exhausts its cursor, excludes hidden
models and requires exactly one visible `isDefault: true`. The rule follows exactly the flag returned
by the CLI; `default` does not independently rank versions or interpret CLI configuration files.
New independent requests follow later default
changes without a bridge restart. Refresh intervals are 5–3600 seconds; maximum age is 5–86400
seconds and must not be shorter than the refresh interval.

The same opt-in also exposes family-policy selectors when their family is available:

| Selector | Rule for new independent requests |
|---|---|
| `default` | Exactly the official visible `isDefault` model |
| `standard` | Latest visible canonical Sol-family generation |
| `advanced` | Latest visible canonical Astra-family generation |
| `fast` | Latest visible canonical Luna-family generation |

Family selection recognizes only `gpt-<numeric version>-sol`, `-astra` or `-luna`, with
decimal components and no leading zeroes except a component equal to zero. Versions are compared
as integer tuples, not strings (`6.10` is newer than `6.9`); trailing zero components are equivalent.
If multiple model identifiers share the latest numeric version, reject the entire catalog update.
Hidden models, preview/other suffixes and unrecognized names do not participate in family selection,
though valid visible names may still have versioned aliases. A missing family omits its selector;
requests for it fail with 400 instead of using another family. Withdrawal may select an older remaining
generation for a fresh request, but never moves a signed continuation off its original model.
These are explicit family policies, not measured speed, price or quality guarantees. `standard`
can differ from `default` when the official default changes family or favors another generation.
Static mode remains unchanged and does not synthesize these selectors.

Versioned aliases are deterministic: remove a leading `gpt-`, replace dots with hyphens and prepend
`codex-` (`gpt-6.1-sol` becomes `codex-6-1-sol`). Other model names retain their full stem after
`codex-`. Invalid identifiers, alias collisions, duplicate models, ambiguous defaults and incomplete
pagination reject the entire update. Discovery is bounded to 128 model records and eight pages.
The existing startup-loaded TOML remains an optional set of pinned compatibility aliases, not an
auto-generated inventory. A pinned alias is advertised only while its target is visible; it is never
repointed to another model. `default`, `standard`, `advanced` and `fast` are reserved in discovery
mode; a TOML override for any of them rejects startup rather than silently changing its meaning.

One immutable catalog generation governs discovery and request acceptance. Each request captures
its effective model and signing authority before awaiting credentials or upstream I/O. Failed refreshes
do not extend the last-good generation's lifetime. With no successful catalog, or after it expires,
`/readyz`, `/v1/models` and generation fail with 503 rather than falling back to static routes.
The catalog cache is memory-only; a restart requires a successful discovery again.

All four selectors' reasoning, compaction and tool continuations carry an authenticated `cobr_t1_`
route receipt inside their existing replayed IDs. The receipt reveals neither the real model name
nor the model-bound signing key; the original ID/state/assistant-semantic signatures remain checked.
After a selector changes target, valid continuations stay on their original model if it remains visible.
Withdrawal, tampering, mixed routes, cross-alias replay and receipt stripping fail closed before
credential/upstream access. A request containing only plain text and no authenticated continuation
artifact cannot prove a previous route and is treated as independent. Preserve the server continuation
key across restarts to retain signed continuity. No conversation history is stored by the bridge.

Catalog discovery does not certify a new model's compatibility with every bridge surface. Existing
strict request, SSE, resource and feature validation remains in force; unsupported provider behavior
is not passed through or retried on another model.

The generated user unit intentionally contains no `EnvironmentFile=` and no secret. For other non-secret overrides, use a user-unit drop-in:

```ini
[Service]
Environment=CODEX_BRIDGE_PORT=8646
```

Create it with `systemctl --user edit codex-openai-bridge.service`, then run `systemctl --user daemon-reload` and restart the service.

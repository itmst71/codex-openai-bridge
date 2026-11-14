# codex-openai-bridge

**English (canonical)** | [日本語](README.ja.md)

> English is canonical; if the Japanese translation differs, English takes precedence.

[![CI](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/downloads/)

Use an OpenAI-compatible client with your existing **official Codex CLI ChatGPT login**.
The local bridge translates Chat Completions and a strict Responses subset to the Codex backend.
It is a protocol adapter, not an agent: your application owns history and executes tools.

`Your application → 127.0.0.1:8646 → Codex backend`

**For personal, single-user, local experimentation only.** Do not share access or operate a
team, hosted, public, commercial, or CI inference service. Use the official OpenAI API for shared
services or production automation. [Service terms and scope](docs/service-terms.md).

Supports text generation, streaming, structured output, and validated tool continuations.
**Not a drop-in replacement for every OpenAI API:** no embeddings, image input, hosted tools,
`previous_response_id`, or server-stored conversations. [Exact API contract](docs/api.md).

## Prerequisites

- Linux, Python **3.12**, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and curl.
- Official OpenAI Codex CLI installed and signed in with ChatGPT using **file** credential storage.
  Follow [Codex CLI setup and credential permissions](docs/setup.md) before starting below.
- An account entitled to the requested Codex models. An OpenAI Developer API key is not a substitute.
- For an existing installation, use [ordinary upgrade instructions](docs/operations.md), not key generation.

## Quickstart

Run these steps on the bridge host, as the same Unix user that owns the CLI login.
The example explicitly enables discovery; the product default remains static mode.

### 1. Install

```bash
git clone https://github.com/itmst71/codex-openai-bridge.git
cd codex-openai-bridge
uv sync --locked
```

### 2. Create credentials once

**Fresh installation only.** This refuses existing files and symlinks; never overwrite keys on an upgrade.
It creates a client token and a separate server signing key without displaying either.

```bash
uv run python - <<'PY'
from pathlib import Path
import os
import secrets

config = Path.home() / ".config"
config.mkdir(mode=0o700, exist_ok=True)
root = config / "codex-openai-bridge"
root.mkdir(mode=0o700, exist_ok=True)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    if os.fstat(directory).st_uid != os.geteuid():
        raise SystemExit("Credential directory must belong to the current user.")
    names = ("client-token", "continuation-key")
    for name in names:
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SystemExit(
                "Existing credentials found; preserve them. Do not regenerate on upgrade."
            )
    values = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    if values[0] == values[1]:
        raise SystemExit("Generated identical keys; nothing written. Try again.")
    os.fchmod(directory, 0o700)
    for name, value in zip(names, values, strict=True):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
finally:
    os.close(directory)
PY
```

### 3. Start in the foreground

```bash
CODEX_BRIDGE_CODEX_PATH="$(command -v codex)" \
CODEX_BRIDGE_MODEL_DISCOVERY=codex \
  uv run python -m codex_openai_bridge serve
```

Keep this terminal open. In another terminal, check readiness:

```bash
curl -q --noproxy '*' --fail --silent --show-error http://127.0.0.1:8646/readyz
```

`/healthz` proves only process liveness. Discovery and generation return 503 until a valid catalog
is available, or if its last-good snapshot expires. Check the CLI login and the configured CLI path
if readiness fails. [Configuration and discovery details](docs/models.md).

### 4. Generate a reply

In the second terminal:

```bash
IFS= read -r bridge_token < "$HOME/.config/codex-openai-bridge/client-token"
curl -q --noproxy '*' --retry 0 --connect-timeout 5 --max-time 245 \
  --fail --silent --show-error \
  -H "Authorization: Bearer $bridge_token" \
  -H 'Content-Type: application/json' \
  --data '{"model":"default","messages":[{"role":"user","content":"Reply briefly."}]}' \
  http://127.0.0.1:8646/v1/chat/completions
unset bridge_token
```

A successful response contains `choices[0].message.content`. This is a real Codex request and
uses your account allowance. The command does not print the token, but same-UID processes may inspect
arguments; that is outside the stated threat model. Stop the foreground bridge with Ctrl-C before
[switching to a systemd user service](docs/operations.md).

## Connect your application

| Setting | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:8646/v1` |
| Client API key / bearer token | Contents of `~/.config/codex-openai-bridge/client-token` |
| Model for the discovery quickstart | `default` |
| SDK automatic retries | Disable (`max_retries=0`) to avoid ambiguous duplicate generations |

The three authorities are different:

| Authority | Used by | Never use it for |
| --- | --- | --- |
| Codex CLI login (`auth.json`) | Official CLI credential helper | Client API keys or public logs |
| `client-token` | Your app's `api_key` / bearer field | Codex login or signing continuations |
| `continuation-key` | Bridge only | Any consumer configuration |

Keep all three private. The app's `api_key` field takes the **bridge token**, not an OpenAI API key.
[Runnable OpenAI Python SDK example](docs/integrations/openai-python.md).

## Choose a model

| Public alias | When available | Selection |
| --- | --- | --- |
| `codex` | Static mode; discovery only while its pinned target is visible | Fixed compatibility route, not automatically newest |
| `default` | Discovery | Official visible `isDefault` model |
| `standard` | Discovery, when Sol is available | Latest canonical Sol-family generation |
| `advanced` | Discovery, when Astra is available | Latest canonical Astra-family generation |
| `fast` | Discovery, when Luna is available | Latest canonical Luna-family generation |

The quickstart enables `CODEX_BRIDGE_MODEL_DISCOVERY=codex`. Without it, the default is static
mode and the compatibility alias is `codex`. Use `/v1/models` to see the actual public aliases.
Family names are selection policies, not measured speed, price, or quality guarantees.
Use the same alias throughout a tool/reasoning/compaction chain. Discovery continuations remain
pinned to their original model; withdrawal is rejected, not silently redirected.
[Static maps, discovery settings, and continuation rules](docs/models.md).

## Consumer compatibility

Evidence levels are not interchangeable: **contract** means a real package against a fixture;
**live** includes real Codex; **operational** adds practical repeated use within the stated scope.
A row never certifies the entire product. Versions, required adapters, and all exclusions are in
[the detailed verification matrix](docs/integrations.md).

| Consumer | Evidence | Main condition / guide |
| --- | --- | --- |
| OpenAI Python SDK | Live | [Disable automatic retries](docs/integrations/openai-python.md) |
| Unhush Custom Formatting | Operational, scoped, user-reported | [Transcript post-processing only; separate STT](docs/integrations/unhush.md) |
| Honcho | Operational | [Text generation only; separate embeddings](docs/integrations/honcho.md) |
| LangChain | Operational, scoped | [Explicit transports; scoped concurrency and persistence](docs/integrations/langchain.md) |
| OpenAI Agents SDK | Live, configured | [Chat Completions model; tracing disabled](docs/integrations/agents.md) |
| AutoGen | Contract, adapter required | [Explicit capabilities and conditional tools adapter](docs/integrations/autogen.md) |
| Aider | Contract, constrained | [One-shot whole-file edit only](docs/integrations/aider.md) |
| Cline CLI | Live, constrained | [Headless three-tool local edit only](docs/integrations/cline.md) |
| Continue core | Contract, component only | [Chat/Edit provider, not the full IDE or CLI](docs/integrations/continue.md) |

## Documentation

- [Setup, login, credential boundaries, and security assumptions](docs/setup.md)
- [API capabilities, rejected fields, and SSE/continuation contracts](docs/api.md)
- [Models, discovery, and environment settings](docs/models.md)
- [systemd, ordinary upgrades, rollback, legacy migrations, and opt-in live tests](docs/operations.md)
- [Consumer versions and exact verification scope](docs/integrations.md)
- [Intended use and service terms](docs/service-terms.md)

## Contributing, security, and license

[Contributing](CONTRIBUTING.md) describes the issue-first process and evidence gates.
Report vulnerabilities privately using [SECURITY.md](SECURITY.md); never attach credentials,
real prompts, tool arguments, or raw upstream responses to public reports.
The [MIT License](LICENSE) covers this source code, not rights to OpenAI services.

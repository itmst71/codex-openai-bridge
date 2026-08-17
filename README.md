# codex-openai-bridge

[![CI](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/downloads/)

A bounded, loopback-only OpenAI-compatible HTTP bridge for the Codex Responses backend. It lets consumers such as the OpenAI Python SDK and Honcho use a stable public model alias (`codex`) while keeping the upstream URL, OAuth authorization, account ID, and real model under server control.

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
| `POST /v1/responses` | Supported, including named Responses SSE events |
| Function/tool calling and tool history | Supported with strict identity checks |
| `text`, `json_object`, and `json_schema` response formats | Supported subject to upstream behavior |
| `max_tokens`, `max_completion_tokens`, and `max_output_tokens` | Accepted and strictly validated for SDK/Honcho compatibility, but not forwarded because the Codex OAuth backend rejects `max_output_tokens` |
| Usage projection and encrypted reasoning round-trip | Supported with bounded validation |
| `POST /v1/embeddings` | **Embeddings are not supported**; returns a sanitized unsupported error |
| Client-selected upstream URL/model/account/auth policy | Unsupported and ignored/rejected |
| Automatic retry of 429, 5xx, timeout, or interrupted generation | Unsupported; only one exact credential refresh/replay after upstream 401 |

The bridge does not claim OpenAI Developer API SLA equivalence, embedding support, or permanent permission to use a ChatGPT/Codex subscription as a service backend. Confirm applicable product terms and operational limits before sustained deployment.

## Security assumptions

- Bind addresses must be loopback IPs. The default and deployed value is `127.0.0.1`.
- Every protected endpoint requires a separate 43-character bridge client token. This token is not the Codex OAuth token.
- The bridge client token is loaded from an owner-only, mode `0600`, non-symlink regular file.
- Codex OAuth credentials are resolved by a bounded subprocess using the Hermes Python environment. They are not copied into this repository or the systemd unit.
- Upstream URL, authorization, account ID, model, prompts, tool arguments, and encrypted reasoning are excluded from operational logs and sanitized errors.
- Request bodies, JSON structure, helper output, upstream bodies, SSE events/streams, queueing, concurrency, deadlines, and shutdown are bounded.
- `ProtectHome=read-only` is relaxed only with `ReadWritePaths=/home/itmst/.hermes` because Hermes may atomically refresh authentication state there.
- The sandbox reduces accidental access and service compromise impact. It does not claim protection from every malicious process already running as the same Unix UID.
- Do not expose port 8646 through a public reverse proxy. If a remote trusted consumer is required, use a separately reviewed authenticated tunnel and preserve the loopback bind.

## Configuration

Create the environment and install the locked project:

```bash
cd /home/itmst/src/codex-openai-bridge
uv sync --locked
```

The service reads validated environment variables. Important settings and defaults:

| Variable | Default | Notes |
| --- | --- | --- |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | Must be an IPv4 or IPv6 loopback address |
| `CODEX_BRIDGE_PORT` | `8646` | TCP port 1–65535 |
| `CODEX_BRIDGE_CLIENT_TOKEN_FILE` | `~/.config/codex-openai-bridge/client-token` | Absolute path; owner, regular file, mode `0600` |
| `CODEX_BRIDGE_UPSTREAM_MODEL` | project default | Canonical server-owned model identifier |
| `CODEX_BRIDGE_HERMES_PYTHON` | `/home/itmst/.hermes/hermes-agent/venv/bin/python` | Hermes credential-helper interpreter |
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
/home/itmst/src/codex-openai-bridge/.venv/bin/python -c \
  'import secrets,sys; sys.stdout.write(secrets.token_urlsafe(32) + "\n")' \
  > "$HOME/.config/codex-openai-bridge/client-token"
chmod 600 "$HOME/.config/codex-openai-bridge/client-token"
```

Keep consumer configuration pointed at the file or inject its value through that consumer's secret mechanism. Never place the value in the repository, unit, journal, or command history.

## Deploy the systemd user service

The host must provide a user manager. Logout/boot persistence additionally requires linger (`loginctl show-user "$USER" -p Linger`). Enabling linger may require an administrator.

Validate and install without starting over an occupied port:

```bash
cd /home/itmst/src/codex-openai-bridge
uv sync --locked
systemd-analyze --user verify deploy/systemd/codex-openai-bridge.service
install -Dm644 deploy/systemd/codex-openai-bridge.service \
  "$HOME/.config/systemd/user/codex-openai-bridge.service"
systemctl --user daemon-reload
systemctl --user enable codex-openai-bridge.service
```

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

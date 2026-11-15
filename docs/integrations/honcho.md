# Honcho

**English (canonical)** | [日本語](honcho.ja.md)

English is canonical if translations differ. [README](../../README.md)

Latest stable server: **3.6.1**, revision `70c67624169683b9e265517aec95a51b8077cabc`, checked
2026-11-15. **Contract verified, native backend/codec only.** The unmodified consumer on Python
3.13.15, OpenAI 3.51.0 and Pydantic 2.14.3 completed six cases against the actual bridge in a
separate Python 3.12.3 process (bridge SDK 1.109.1 / Pydantic 2.13.4). All seven HTTP requests
returned 200 against a synthetic upstream; both declared runtime ranges were respected.
The earlier 3.13-bridge protocol probe and four upstream normalization regressions remain separate
evidence. No real provider, DB/Redis or full server was run.
The nullable tool-content fix tracked by #1061 is included in this release and passed the direct
codec continuation case; the old revision's required patch is not a current 3.6.1 requirement.
See the [dated latest and historical matrix](../integrations.md).

Point Honcho's **text generation** modules at the bridge and keep embeddings on a distinct provider. Apply the same override to summary, dream, derivation, and dialectic model configurations that should use Codex.

```toml
[deriver.model_config]
transport = "openai"
model = "codex"

[deriver.model_config.overrides]
base_url = "http://127.0.0.1:8646/v1"
api_key_env = "CODEX_BRIDGE_CLIENT_TOKEN"
```

Keep native `json_schema` structured output for the verified role. The older full-service baseline
observed upstream 502 for `structured_output_mode = "json_object"`, while the same derivation
succeeded with native schema. That is historical provider evidence, not a new 3.6.1 live test.
The latest backend tests cover native schema with/without tools and streamed schema strings; the
stream itself is not claimed to return a Pydantic-parsed object. The full-service recipe above was
not rerun as a 3.6.1 deployment.

Use a separate OpenAI-compatible embedding backend with its own base URL and `api_key_env`. For an
initial bridge-only experiment where missing embeddings are acceptable, explicitly set:

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

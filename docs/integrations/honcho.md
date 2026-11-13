# Honcho

**English (canonical)** | [日本語](honcho.ja.md)

English is canonical if translations differ. [README](../../README.md)

Point Honcho's **text generation** modules at the bridge and keep embeddings on a distinct provider. Apply the same override to summary, dream, derivation, and dialectic model configurations that should use Codex.

```toml
[deriver.model_config]
transport = "openai"
model = "codex"

[deriver.model_config.overrides]
base_url = "http://127.0.0.1:8646/v1"
api_key_env = "CODEX_BRIDGE_CLIENT_TOKEN"
```

Keep Honcho's default native `json_schema` structured-output mode for the Codex bridge. Do not set
`structured_output_mode = "json_object"`: bounded live probes consistently returned an upstream 502
for `json_object`, while the same derivation completed successfully with native `json_schema`.

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

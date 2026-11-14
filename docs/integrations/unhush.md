# Unhush Custom Formatting

**English (canonical)** | [日本語](unhush.ja.md)

English is canonical if translations differ. [README](../../README.md)

## Evidence and scope

**Operationally verified (scoped, user-reported).** The operator reports successful practical use of
Unhush's Custom Formatting with this bridge. The observed reporting environment uses the Linux
AppImage package **3.1.0**; that version was read from the packaged application metadata.
This is a user-reported integration, not independently reverified end-to-end operational evidence.
It does not certify every 3.1.0 build, another platform/version, long-session behavior, restart or
failure recovery, keyboard injection, or every prompt/model combination.

Source inspection confirms the formatting request shape below. Deterministic tests exercise the
strict bridge with synthetic credentials and a fake upstream; they are not additional live-Codex
or microphone/desktop verification. Unhush is not a bridge runtime or CI package dependency.

## Configure formatting, not transcription

Start the bridge separately using the [quickstart](../../README.md). In Unhush Settings, select
**Custom** for **Formatting**, not for the speech-recognition provider. Use these values:

| Formatting setting | Stored key | Value |
| --- | --- | --- |
| Provider | `llm_provider` | `custom` |
| Server URL | `llm_custom_url` | `http://127.0.0.1:8646` — without `/v1` |
| API Key | `llm_custom_key` | Contents of `~/.config/codex-openai-bridge/client-token` |
| Model | `llm_model_custom` | A public alias listed by `/v1/models`; `default` for the discovery quickstart |
| Start Command | `llm_custom_start_cmd` | Leave blank when systemd or another manager owns the bridge |

Unhush builds `/v1/models` and `/v1/chat/completions` from the server prefix. Do not enter a full
Chat endpoint as the recommended Server URL. In static mode use an actually configured alias,
normally `codex`; discovery aliases such as `default` require discovery to be enabled.
See [model selection](../models.md).

Although a generic Custom API Key field can be marked optional, this bridge requires its bearer
token. Never use the Codex CLI OAuth credentials or `continuation-key` in that field.
Use the UI to change only Formatting. The stored-key column is a reference, not a replacement
`settings.json`: preserve unrelated settings and the existing transcription provider.
Settings and Chromium local storage can contain the client token; keep them private and never
include their contents in issues or screenshots.

## Request contract

Formatting uses non-streaming Chat Completions: one system message, a transcript-wrapped user
message with final instructions, the selected public model, and a positive `max_tokens` value.
A bounded illustrative request is:

```json
{
  "model": "default",
  "max_tokens": 256,
  "messages": [
    {"role": "system", "content": "Format the transcript without changing its meaning."},
    {"role": "user", "content": "<transcript>\nA short synthetic transcript.\n</transcript>\n\nOutput the cleaned transcript only."}
  ]
}
```

The formatter expects nonempty `choices[0].message.content`; model discovery or an HTTP 200 with
empty content is not a successful formatting operation. Its client request timeout is **90 seconds**.
The current formatter uses at least 256 requested tokens; its separate warm-up uses `max_tokens: 1`.
The bridge validates these compatibility fields but does **not** forward `max_tokens` as an upstream
output cap. Keep Unhush's output-length guard enabled; do not treat this field as a hard provider limit.
The payload is an API illustration, not Unhush's complete settings file.

## Speech recognition and privacy

This bridge is **not a speech-to-text backend** and does not provide `/v1/audio/transcriptions`.
Leave microphone input and STT with a separate provider; a local Whisper-compatible service is one
possible choice, not a new requirement or a capability supplied by this bridge.

Even with local speech recognition, **transcript text leaves the host** when Formatting sends it
through the bridge to the cloud-backed Codex service. This is not a fully local/offline pipeline.
Only send text you intend to disclose to that service. The bridge's personal, single-user scope and
[service terms](../service-terms.md) still apply.

## Check and diagnose

1. Verify bridge readiness, then confirm the chosen public model appears in Unhush's model list.
2. Try a short, non-sensitive transcript and require an actual nonempty formatted result.
3. On 401, check the bridge client token without printing it. On a model error, check the alias and
   discovery/static mode. On a timeout, remember the formatter's 90-second client deadline.
4. Do not interpret fallback to the raw transcript as successful formatting. Diagnose formatting
   separately from microphone capture, STT, clipboard output, and desktop typing.

The operator's practical-use report is the basis for the operational label. This guide does not
claim an independently repeated full Unhush UI workflow or failure-recovery campaign.

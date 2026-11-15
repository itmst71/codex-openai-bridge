# Unhush Custom Formatting

**English (canonical)** | [日本語](unhush.ja.md)

English is canonical if translations differ. [README](../../README.md)

## Evidence and scope

**Operationally verified (scoped, maintainer-executed).** Source-locked contracts and an isolated
Linux AppImage **3.1.0** packaged-renderer trial now supplement the earlier user-reported evidence.
The live trial used `default` → `gpt-6.1-sol` through a strict isolated bridge and real Codex.
The exact source revision, package hash, scenarios and exclusions are in the
[frozen evidence summary](unhush-evidence.json).

This is **not a full microphone-to-desktop test**. A **recorder-completion fixture** supplied synthetic
transcripts to the actual packaged stop/formatting code, without rewriting that code. Output was
read back only on a private Xvfb clipboard. Existing app profiles, desktop input and microphones
were not used. It does not certify other builds/platforms/models, arbitrary prompt accuracy,
normal desktop clipboard/typing/hotkeys, unrelated UI flows, or long-session operation.

Unhush is not a bridge runtime dependency. CI separately prepares the pinned consumer source with
an optional locked compiler; deterministic contracts use synthetic credentials and a fake upstream.

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

The UI persists changes in Chromium local storage. A startup `settings.json`, if present, can
override those values on launch; it is not a UI save target. The restart trial reused its owned
profile after removing only its synthetic seed override. Do not delete your own settings file to
imitate that fixture; account for any deliberate startup overrides.

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

## Bounded maintainer trial

| Check | Evidence |
| --- | --- |
| Warm-up and repetition | Actual user-only `max_tokens: 1` warm-up; three fixed-literal formatting repeats through real Codex |
| Failure handling | Local gateway injected 401, 429, 503, empty content and malformed JSON; actual stop path returned raw text, then real formatting recovered in the same renderer |
| Output length | Exact boundary accepted; oversized output discarded for raw text |
| Deadline | Native 90-second abort observed at 90,008 ms, without a shortened clock; raw fallback and a subsequent real formatting request succeeded |
| Persistence | Real Formatting UI keyboard edit/save, page reload and full owned app restart; prompt/provider/model/token read back, then real warm-up and formatting succeeded |

Faults were injected locally, not observed provider outages. The recorder boundary remained a
fixture, and this was finite sequential work, not a longevity or concurrency campaign. Two early
prompt-specific assertions failed: an extraction prompt yielded empty text, and a conversion
prompt yielded XML-wrapped text rejected by the length guard. Both raw fallbacks were retained in
the evidence; the repeat/recovery scenario used a separate explicit fixed-literal prompt. No failed
request was silently retried as success, and no general prompt-quality claim follows.

The trial also found a shared bridge 502: current Codex returned a private
`usage.input_tokens_details.cache_write_tokens_details` object. This change validates only the
observed closed two-counter shape and strips it before public output; unknown fields still fail
closed. The nested extension is observed provider behavior, not a published official schema or a
new public usage capability. Model discovery in this trial used a public official-CLI preflight
snapshot to keep the live fixture's CLI home read-only; dynamic refresh was not reverified here.

## Reproduce the optional consumer contract

From this checkout, use Node 20 or later and an owned scratch directory in `TMPDIR`. The revision
and four source hashes are pinned in `tests/consumer_contract/unhush_env/source-lock.json`.
The preparation step rejects a modified cache instead of replacing it. No live credentials,
installed Unhush profile or microphone are needed:

```bash
prefix="$(mktemp -d "${TMPDIR:?Set TMPDIR to an owned scratch directory}/unhush-contract.XXXXXX")"
cp tests/consumer_contract/unhush_env/package.json \
   tests/consumer_contract/unhush_env/package-lock.json "$prefix/"
npm ci --prefix "$prefix" --ignore-scripts --no-audit --no-fund
node tests/consumer_contract/unhush_env/prepare.mjs "$prefix"
UNHUSH_CONTRACT_PREFIX="$prefix" uv run pytest -q tests/consumer_contract/test_unhush.py
```

This reproduces the deterministic consumer contract, not the packaged live trial. The latter
requires the exact package, a separate user-data directory/display, synthetic recorder completion,
an isolated bridge and a controlled fault gateway; never attach its probes to an existing profile.
The pinned tests and compiler are optional test tooling, not production dependencies.

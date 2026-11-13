# Aider

**English (canonical)** | [日本語](aider.ja.md)

English is canonical if translations differ. [README](../../README.md)

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

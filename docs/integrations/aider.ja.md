# Aider

[English (canonical)](aider.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

Aider は OpenAI-compatible Chat Completions endpoint を使用します。未知の public alias は、target repository の外に置く non-secret model settings file に登録してください。たとえば `$HOME/.config/codex-openai-bridge/aider-model-settings.yml` を使用すると、Aider が非対応の sampling control を送信しなくなります。

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

Aider は、local definition が与えられない限り、unknown-model metadata の更新も試みます。2 つ目の non-secret file は target repository の外に保存します。たとえば `$HOME/.config/codex-openai-bridge/aider-model-metadata.json` とします。

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

再現可能な形で検証済みの role は one-shot edit であり、Aider の default interactive mode ではありません。次の例では Aider state を target repository の外に置き、contract が対象とする side effects を無効にし、LiteLLM に bundled model-cost map の使用を強制します。

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

恒常的な contract では、実際の Aider CLI を `--message` mode で駆動し、production-like loopback ブリッジから streaming whole-file listing 1 件を受信し、tracked file を正確に 1 個だけ編集します。repository artifact は作成せず、Git HEAD は変更しません。Aider 0.86.2 には独自の制限付き transient-API retry loop があり、他のコンシューマー向けの OpenAI SDK `max_retries=0` guidance とは別物です。検証済みの Aider role は text editing のみを行い、model tool calls や auto-commit side effect はありません。Architect mode、weak/editor model flows、repo map、auto-commit、lint/test repair loops、`diff`/`udiff` formats、image input、interactive multi-turn behavior は、それぞれテストされるまで対応を保証しません。

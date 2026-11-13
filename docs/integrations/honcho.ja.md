# Honcho

[English (canonical)](honcho.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

Honcho の **text generation** modules はブリッジに向け、embeddings には別の provider を使用してください。Codex を使用する summary、dream、derivation、dialectic model configurations にも、同じ override を適用します。

```toml
[deriver.model_config]
transport = "openai"
model = "codex"

[deriver.model_config.overrides]
base_url = "http://127.0.0.1:8646/v1"
api_key_env = "CODEX_BRIDGE_CLIENT_TOKEN"
```

Codexブリッジでは、Honcho既定のnative `json_schema` structured-output modeを維持してください。
`structured_output_mode = "json_object"`は設定しないでください。制限付きのlive probeでは
`json_object`が一貫してupstream 502を返した一方、同じderivationはnative `json_schema`で
正常に完了しました。

独自の base URL と `api_key_env` を持つ、別の OpenAI-compatible embedding backend を使用してください。embeddings がなくてもよいブリッジのみの初期実験では、次を明示的に設定します。

```bash
EMBED_MESSAGES=false
```

Honcho embedding requests をこのブリッジに向けないでください。

現在の Honcho は、text calls で token-limit field を送信します。ブリッジはその field を検証しますが、ChatGPT Codex Responses backend が `max_output_tokens` を拒否するため、generation 時に上限を適用できません。それでもブリッジは、設定済みの total deadline、upstream/downstream byte caps、SSE-event cap、JSON bounds を適用します。このブリッジを text backend として使用する場合、Honcho の `max_tokens` 値を厳密な generation limit として扱わないでください。

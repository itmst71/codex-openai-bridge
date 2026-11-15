# Honcho

[English (canonical)](honcho.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

最新stable serverは**3.6.1**、revision `70c67624169683b9e265517aec95a51b8077cabc`、確認日は2026-11-15です。
**Contract検証済み（native backend/codecのみ）**です。未修正consumerをPython3.13.15・OpenAI3.51.0・Pydantic2.14.3で実行し、
別processの実bridge（Python3.12.3・SDK1.109.1・Pydantic2.13.4）に対する6件が成功しました。
synthetic upstreamに対するHTTP7件は全200で、両者の宣言runtimeを満たしています。
初回3.13-bridge protocol probeと上流正規化回帰4件は別の証拠として保持しています。
実provider・DB/Redis・server全体は起動していません。#1061のnullable tool-content修正は本releaseに含まれ、
実codec continuationでも通過しています。旧revisionの必要patchを3.6.1にも要求するものではありません。
[確認日付きの最新／過去の検証表](../integrations.ja.md)を参照してください。

Honcho の **text generation** modules はブリッジに向け、embeddings には別の provider を使用してください。Codex を使用する summary、dream、derivation、dialectic model configurations にも、同じ override を適用します。

```toml
[deriver.model_config]
transport = "openai"
model = "codex"

[deriver.model_config.overrides]
base_url = "http://127.0.0.1:8646/v1"
api_key_env = "CODEX_BRIDGE_CLIENT_TOKEN"
```

検証済みroleではnative `json_schema`を維持してください。旧full-service baselineでは
`structured_output_mode = "json_object"`がupstream502、同じderivationのnative schemaが成功でしたが、
これは旧版でのprovider実測であり、3.6.1の新しいlive試験ではありません。
最新版backend試験はtools有無のnative schemaとstreamed schema文字列を対象とし、stream自体のPydantic parseは保証しません。
上のfull-service設定例を3.6.1 deploymentとして再実行したわけではありません。

独自の base URL と `api_key_env` を持つ、別の OpenAI-compatible embedding backend を使用してください。embeddings がなくてもよいブリッジのみの初期実験では、次を明示的に設定します。

```bash
EMBED_MESSAGES=false
```

Honcho embedding requests をこのブリッジに向けないでください。

現在の Honcho は、text calls で token-limit field を送信します。ブリッジはその field を検証しますが、ChatGPT Codex Responses backend が `max_output_tokens` を拒否するため、generation 時に上限を適用できません。それでもブリッジは、設定済みの total deadline、upstream/downstream byte caps、SSE-event cap、JSON bounds を適用します。このブリッジを text backend として使用する場合、Honcho の `max_tokens` 値を厳密な generation limit として扱わないでください。

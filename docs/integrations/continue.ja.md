# Continue core OpenAI provider

[English (canonical)](continue.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

Continue は model roles を分離するため、ブリッジは text generation を担当し、embeddings と autocomplete には他の providers を使用してください。対応する local model entry は次のとおりです。

```yaml
models:
  - name: Codex Bridge
    provider: openai
    model: codex
    apiBase: http://127.0.0.1:8646/v1
    apiKey: ${{ secrets.OPENAI_API_KEY }}
    useLegacyCompletionsEndpoint: false
    roles:
      - chat
      - edit
    defaultCompletionOptions:
      maxTokens: 4096
```

この model に `temperature`、`topP`、penalties、stop words、`autocomplete`、`embed` を追加しないでください。検証済みの package contract は、Continue core の公開 `streamChat` と `streamComplete` methods を呼び出し、streaming Chat と Edit-role generation を実行し、function-tool call/result/final-answer round-trip を完了します。4 つの requests はすべて `/v1/chat/completions` を使用し、ブリッジの server-owned policy を維持し、non-loopback connection を行いません。

これは、Continue IDE 全体または現在の `cn` CLI への対応を示すものではありません。read-only headless の `@continuedev/cli` 1.5.47 probe はブリッジ経由の generation に成功しましたが、startup 時に `api.continue.dev` への接続も試みました。このため、対応済みの local-only control-plane configuration が利用可能になるまでは未検証です。Autocomplete/FIM、embeddings、reranking、file application、agent tools、image input、Responses API selection も同様に対応を保証しません。

公開されている `@continuedev/core` 1.1.0 compiled ESM subpath は、内部 import の 1 つに file extension がないため、レビュー対象の Node 20/24 runtimes では単独で import できません。そのため contract は、固定した `tsx` を介して、その正確な package に同梱された TypeScript source を実行します。これは Continue 自身の build が使用する source でもあります。完全な optional npm graph は、consumer-contract `package-lock.json` によって固定されます。lifecycle scripts は無効であり、使用しない native SQLite logger dependency は、その logging method を probe で無効にする前に、固定したコンシューマー専用 stub へ置き換えられます。これらの packages がブリッジ runtime または development dependencies になることはありません。Continue で成功した残りの requests の local logs は、CI 内の isolated HOME に限定されます。telemetry は無効で、self-tested socket guard が external network access を拒否します。

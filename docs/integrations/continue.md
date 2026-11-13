# Continue core OpenAI provider

**English (canonical)** | [日本語](continue.ja.md)

English is canonical if translations differ. [README](../../README.md)

Continue separates model roles, so the bridge should serve text generation while embeddings and
autocomplete use other providers. A matching local model entry is:

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

Do not add `temperature`, `topP`, penalties, stop words, `autocomplete`, or `embed` to this model.
The verified package contract invokes Continue core's public `streamChat` and `streamComplete`
methods, exercises streaming Chat and Edit-role generation, and completes a function-tool
call/result/final-answer round-trip. All four requests use `/v1/chat/completions`, preserve the
bridge's server-owned policy, and make no non-loopback connection.

This is not yet a claim for the full Continue IDE or the current `cn` CLI. A read-only headless
`@continuedev/cli` 1.5.47 probe successfully generated through the bridge but also attempted to
contact `api.continue.dev` during startup, so it remains unverified until a supported local-only
control-plane configuration is available. Autocomplete/FIM, embeddings, reranking, file application,
agent tools, image input, and Responses API selection are likewise unclaimed.

The published `@continuedev/core` 1.1.0 compiled ESM subpath is not standalone-importable on the
reviewed Node 20/24 runtimes because one internal import lacks a file extension. The contract
therefore executes the TypeScript source shipped in that exact package through pinned
`tsx`, which is also the source consumed by Continue's own build. Its complete optional npm graph is
frozen by the consumer-contract `package-lock.json`; lifecycle scripts are disabled, and the unused
native SQLite logger dependency is replaced by a locked consumer-only stub before its logging method
is disabled by the probe. None of those packages become bridge runtime or development dependencies.
Continue's remaining successful-request local logs are confined to an isolated HOME in CI; telemetry
is disabled and a self-tested socket guard rejects external network access.

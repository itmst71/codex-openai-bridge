# Unhush Custom Formatting

[English (canonical)](unhush.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

## 根拠と対象範囲

**最新安定版のsource client: Live verified（範囲限定）、3.3.4**。確認日は2026-11-15です。
新しい隔離prefixでhash固定sourceのconsumer contractを13件実行し、warm-up Offの`skipped`状態・model POSTなしも確認しました。
実Codexのformatter呼出しは3回成功しています。[最新安定版の証跡](latest-stable-evidence.json)を参照してください。
旧実package試験を3.3.4へ移転するものではありません。新版のRecordingBar fallback・length guard、実90秒timeout、
UI永続化・app再起動・desktop出力は再検証していません。最新AppImageはhash確認・展開のみで、起動していません。
fixtureでは両方のStart Command APIを禁止しています。

### 過去の実package baseline — 3.1.0

**Operationally verified（範囲限定・maintainer-executed）**。従来のuser-reported evidenceに加え、
sourceを固定したcontractと、隔離したLinux AppImage **3.1.0**のpackaged renderer試験を実施しました。
liveではstrictな隔離bridgeと実Codexを通し、`default` → `gpt-6.1-sol`を使いました。
source revision、package hash、scenarioと対象外は[凍結した証跡要約](unhush-evidence.json)に記録しています。

これは**マイクからdesktop出力までの全体検証ではありません**。**録音完了を置き換えるfixture**で合成transcriptを
実packageの停止・整形codeに渡し、そのcode自体は書き換えていません。出力のreadbackも独立Xvfbのclipboardだけです。
既存app profile、通常desktop入力、microphoneは使っていません。他build/platform/model、任意promptの精度、
通常desktopのclipboard/typing/hotkey、無関係なUI経路、長時間運用を保証しません。

Unhushはbridgeのruntime依存ではありません。CIではoptionalなlocked compilerを使って固定consumer sourceを別途準備し、
決定的contractをsynthetic credentialsとfake upstreamで実行します。

## 音声認識ではなく整形を設定

[クイックスタート](../../README.ja.md)に従ってbridgeを別途起動します。UnhushのSettingsでは
音声認識providerではなく、**Formatting**で**Custom**を選び、以下を設定してください。

| Formattingの設定 | 保存key | 値 |
| --- | --- | --- |
| Provider | `llm_provider` | `custom` |
| Server URL | `llm_custom_url` | `http://127.0.0.1:8646` — `/v1`を付けない |
| API Key | `llm_custom_key` | `~/.config/codex-openai-bridge/client-token`の内容 |
| Model | `llm_model_custom` | `/v1/models`に掲載される公開alias。discoveryのクイックスタートでは`default` |
| Start Command | `llm_custom_start_cmd` | systemdなどがbridgeを管理する場合は空欄 |

Unhushはserver prefixから`/v1/models`と`/v1/chat/completions`を組み立てます。
推奨のServer URLにはChat endpoint全体を入力しないでください。static modeでは通常の`codex`など、
実際に設定されたaliasを使います。`default`などのdiscovery aliasはdiscoveryを有効にした場合だけ利用できます。
[モデル選択](../models.ja.md)も参照してください。

汎用Custom API Key欄がoptionalと表示されても、このbridgeにはbearer tokenが必要です。
Codex CLIのOAuth認証情報や`continuation-key`をその欄へ入れてはいけません。
UIでFormattingだけを変更してください。保存keyの列は参照用であり、`settings.json`全体の置換例ではありません。
他の設定と既存の音声認識providerを維持してください。
SettingsとChromium local storageにはclient tokenが含まれることがあります。非公開で保管し、
内容をissueやscreenshotへ添付しないでください。

UIの変更先はChromium local storageです。起動用`settings.json`がある場合は、その値が起動時に上書きされることがあり、
UIの保存先ではありません。再起動試験では所有するprofileを再利用し、合成seed overrideだけを取り除きました。
fixtureを再現するために自分の設定fileを削除せず、意図した起動時overrideの有無を考慮してください。

## リクエスト契約

整形はnon-streaming Chat Completionsを使います。system message 1個、transcriptを囲んで末尾指示を加えた
user message 1個、選択した公開model、正の`max_tokens`を送信します。
短いrequestの例は次のとおりです。

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

formatterは空ではない`choices[0].message.content`を必要とします。model一覧の取得や、contentが空のHTTP 200は
整形成功を意味しません。client requestのtimeoutは**90秒**です。
現在のformatterは最低256 requested tokensを指定し、別のwarm-upでは`max_tokens: 1`を使います。
bridgeは互換fieldとして検証しますが、`max_tokens`をupstream出力上限として**転送しません**。
Unhushの出力長guardを有効なままにし、このfieldをproviderの厳密な上限と解釈しないでください。
このpayloadはAPIの説明用であり、Unhushの設定file全体ではありません。

## 音声認識とプライバシー

このbridgeは**音声認識バックエンドではありません**。`/v1/audio/transcriptions`を提供しません。
microphone入力とSTTは別providerに任せてください。local Whisper互換serviceは選択肢の1つであり、
新しい必須依存でも、このbridgeが提供する機能でもありません。

音声認識がlocalでも、Formattingからbridgeを通してcloud-backed Codexへ送る**テキストはホストの外へ送信**されます。
完全local/offlineのpipelineではありません。そのserviceへ渡してよいtextだけを送ってください。
bridgeの個人・単一ユーザーの範囲と[サービス利用条件](../service-terms.ja.md)は引き続き適用されます。

## 確認と切り分け

1. bridgeのreadinessを確認し、選んだ公開modelがUnhushのmodel一覧にあることを確認します。
2. 機密情報を含まない短いtranscriptで試し、実際に空でない整形結果が返ることを確認します。
3. 401ではbridge client tokenを表示せずに確認します。model errorではaliasとdiscovery/static modeを確認します。
   timeoutではformatter側の90秒のclient deadlineに注意してください。
4. raw transcriptへのfallbackを整形成功と誤認しないでください。Formattingを、microphone capture・STT・
   clipboard出力・desktop typingと分けて切り分けます。

## 過去の範囲限定maintainer試験 — 3.1.0

| 確認 | 証拠 |
| --- | --- |
| Warm-up・反復 | 実際のuser-only `max_tokens: 1` warm-upと、実Codex経由の固定literal整形3回 |
| 障害処理 | local gatewayで401・429・503・空content・不正JSONを注入。実停止経路がraw textへ戻り、同じrendererで実整形へ復帰 |
| 出力長 | 境界値は受入、超過時は出力を破棄してraw textを使用 |
| Deadline | 時計を短縮せずnative 90秒abortを90,008 msで観測。raw fallback後の実整形requestも成功 |
| 永続化 | 実Formatting UIをkeyboardで変更・保存し、page reloadと所有app全体の再起動でprompt/provider/model/tokenをreadback。その後の実warm-upと整形も成功 |

障害はlocal注入であり、実provider障害の観測ではありません。recorder境界はfixtureのままで、有限・逐次試験であり、
長期運用や並列性の試験でもありません。初期のprompt別assertionでは、抽出指示が空textを返したケースと、変換指示が
XML付きtextを返して出力長guardに拒否されたケースがありました。両方のraw fallbackを証跡に残し、反復・復帰scenarioは
別の明示的な固定literal promptで実行しました。失敗requestを黙って再試行して成功扱いにせず、一般的なprompt品質も保証しません。

この試験ではbridge共通の502も検出しました。現在のCodexがprivateな
`usage.input_tokens_details.cache_write_tokens_details` objectを返すためで、この変更では実観測した2-counterの
閉じたshapeだけを検証し、公開出力前に除去します。未知fieldは引き続きfail-closedです。このnested拡張はproviderの
実観測であり、公開された公式schemaや新しい公開usage機能ではありません。live fixtureのCLI homeをread-onlyにするため、
model discoveryには公式CLI preflightから得た公開snapshotを使いました。動的refreshの再検証ではありません。

## optional consumer contractの再現

checkoutからNode 20以降と、`TMPDIR`に指定した所有scratch directoryを使います。revisionと4 source hashは
`tests/consumer_contract/unhush_env/source-lock.json`で固定し、改変cacheは置換せず拒否します。
現在のpinは3.3.4で、consumer 13件を実行します。旧3.1.0の実package受入試験とは別です。
実credential、インストール済みUnhushのprofile、microphoneは不要です。

```bash
prefix="$(mktemp -d "${TMPDIR:?Set TMPDIR to an owned scratch directory}/unhush-contract.XXXXXX")"
cp tests/consumer_contract/unhush_env/package.json \
   tests/consumer_contract/unhush_env/package-lock.json "$prefix/"
npm ci --prefix "$prefix" --ignore-scripts --no-audit --no-fund
node tests/consumer_contract/unhush_env/prepare.mjs "$prefix"
UNHUSH_CONTRACT_PREFIX="$prefix" uv run pytest -q tests/consumer_contract/test_unhush.py
```

これは決定的consumer contractの再現であり、実packageのlive試験ではありません。後者には同じpackage、別user-data
directoryとdisplay、合成recorder completion、隔離bridge、制御したfault gatewayが必要です。既存profileへprobeを接続しないでください。
固定testsとcompilerはoptional test toolingであり、production依存ではありません。

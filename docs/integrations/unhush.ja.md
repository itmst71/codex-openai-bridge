# Unhush Custom Formatting

[English (canonical)](unhush.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

## 根拠と対象範囲

**Operationally verified（範囲限定・user-reported）**。利用者から、UnhushのCustom Formattingと
このbridgeを組み合わせた実利用で正常に動作しているとの報告があります。報告環境で確認したpackageは
Linux AppImage **3.1.0**で、版数はpackage内のapplication metadataから読み取りました。
これは利用者の実利用報告であり、独立した実運用再検証ではありません。
すべての3.1.0 build、他のplatform/version、長時間利用、再起動・障害復旧、keyboard injection、
あらゆるprompt/modelの組み合わせを保証しません。

sourceで以下の整形request形状を確認しています。決定的なtestではsynthetic credentialsとfake upstreamを
使ってstrict bridgeを実行しますが、追加のlive Codex検証やmicrophone/desktop検証ではありません。
Unhushをbridgeのruntime依存やCI package依存に追加するものではありません。

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

実運用labelの根拠は利用者の実利用報告です。Unhush UI全体の反復workflowや障害復旧試験を、
独立して実施済みと主張するguideではありません。

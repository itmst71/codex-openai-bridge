# モデルと設定リファレンス

[English (canonical)](models.md) | **日本語**

英語版を正本（canonical）とします。 [README](../README.ja.md)

environment を作成し、固定された project をインストールします。

```bash
git clone https://github.com/itmst71/codex-openai-bridge.git
cd codex-openai-bridge
uv sync --locked
```

service は、検証済みの environment variables を読み込みます。主な settings と defaults は次のとおりです。

| 変数 | デフォルト | 注記 |
| --- | --- | --- |
| `CODEX_BRIDGE_HOST` | `127.0.0.1` | IPv4 または IPv6 の loopback address でなければならない |
| `CODEX_BRIDGE_PORT` | `8646` | TCP port 1–65535 |
| `CODEX_BRIDGE_CLIENT_TOKEN_FILE` | `~/.config/codex-openai-bridge/client-token` | Absolute path。owner、regular file、mode `0600` |
| `CODEX_BRIDGE_CONTINUATION_KEY_FILE` | `~/.config/codex-openai-bridge/continuation-key` | server-only continuation署名key。clientへ渡さない |
| `CODEX_BRIDGE_MODEL_CONFIG_FILE` | 存在する場合`~/.config/codex-openai-bridge/models.toml` | owner管理alias mapのoptional absolute path |
| `CODEX_BRIDGE_UPSTREAM_MODEL` | project default | 従来single-alias model。model mapが存在する場合は拒否 |
| `CODEX_BRIDGE_CODEX_PATH` | `$HOME/.local/bin/codex` | OpenAI公式Codex CLI executableのabsolute path |
| `CODEX_BRIDGE_CODEX_HOME` | `$HOME/.codex` | file-mode `auth.json`を含むCodex CLI homeのabsolute path |
| `CODEX_BRIDGE_MAX_IN_FLIGHT` | `2` | 同時実行 owners の上限。最大 10 |
| `CODEX_BRIDGE_QUEUE_WAIT_SECONDS` | `10` | admission wait の上限 |
| `CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS` | `240` | downstream writes を含む request 全体 |

追加の `CODEX_BRIDGE_MAX_*` と timeout variables は、`config.py` で厳格に制限されています。無効、noncanonical、non-loopback、または不整合な settings がある場合、起動に失敗します。



## Server-owned model aliases

以下はデフォルトの**静的モード**の説明です。

必須の`codex`以外も有効にするにはexampleをcopyします。

```bash
install -d -m 700 "$HOME/.config/codex-openai-bridge"
umask 077
(set -C; cat deploy/models.toml.example > "$HOME/.config/codex-openai-bridge/models.toml")
```

```toml
version = 1

[models]
codex = "gpt-5.6-terra"
codex-sol = "gpt-5.6-sol"
```

左辺はclientから受理するserverが承認したalias、右辺はserver管理の実upstream modelです。`codex`は必須で、aliasは最大16件です。未知key、不正identifier、symlink、hard link、不安定なread、group/world-writableなpath authorityは起動時に拒否します。別のabsolute pathは`CODEX_BRIDGE_MODEL_CONFIG_FILE`で指定できます。Model mapと`CODEX_BRIDGE_UPSTREAM_MODEL`は競合authorityなので併用できません。

Mapは起動時に一度だけ読み込みfreezeします。編集後はbridgeを再起動してください。in-flight requestが変更後mappingを観測することはありません。既存aliasの実modelを変更すると、以前のcontinuation stateは意図的にfail-closedで無効になります。`/v1/models`は`codex`を先頭に、残りを辞書順で返します。Responseはclientが選択したaliasを返し、実upstream model identifierを返しません。

tool、reasoning、compaction continuation全体で同じaliasを使用してください。署名付きpublic continuation IDとopaque-state envelopeが、選択aliasと設定済み実modelの両方をbindし、cross-alias continuationはupstream access前に拒否します。Model mapを有効にする前にactiveな旧single-alias chainを完了してください。Modelを変える場合は現在のchainを完了するか、public textによる明示handoffでfresh chainを開始します。

これらのoperator定義aliasはprojectのsupport claimではありません。Production処理へ割り当てる前に、使用するChat、Responses、streaming、structured output、tool、reasoning、compactionの各surfaceで実modelを個別にlive検証してください。

## 公式カタログとdefaultの自動選択

モデル追加・撤去・既定変更へ自動追従する場合は、user-unit drop-inで明示的に有効化します。

```ini
[Service]
Environment=CODEX_BRIDGE_MODEL_DISCOVERY=codex
Environment=CODEX_BRIDGE_MODEL_REFRESH_SECONDS=300
Environment=CODEX_BRIDGE_MODEL_MAX_AGE_SECONDS=1800
```

一度再起動した後は、clientが`model="default"`を指定し続けられます。公式CLIの`model/list`をcursor末尾まで取得し、非hiddenの中で一意な`isDefault: true`を採用します。`default`はCLIが返すflagを正本とし、独自の世代順位付けやCLI設定fileの別解釈はしません。取得はaccountに結び付け、stdout/stderr・deadline・子process groupのcleanupを制限します。

同じ自動モードで、利用可能な系列に応じて以下も公開します。

| selector | 新しい独立requestの選択規則 |
|---|---|
| `default` | 公式の一意な非hidden `isDefault` |
| `standard` | Sol系列の最新の公開世代 |
| `advanced` | Astra系列の最新の公開世代 |
| `fast` | Luna系列の最新の公開世代 |

系列判定は`gpt-<数値version>-sol`・`-astra`・`-luna`の完全一致だけです。versionは先頭ゼロのない十進整数成分（ゼロ自体は可）を数値tupleで比較し、`6.10`は`6.9`より新しく扱います。末尾のゼロ成分は同値です。最新数値versionに異なるmodel IDが並ぶ場合は更新全体を拒否します。hidden、preview等の別suffix、未認識名は系列選択から除外しますが、安全な公開名の世代aliasは残せます。

該当系列がなければそのselectorを一覧から除き、要求は400で拒否します。別系列へのfallbackはしません。最新model撤去後の新規requestは同系列の残存旧世代を選べますが、署名付き継続は開始modelが撤去されたら拒否します。これは系列選択方針であり、実測の速度・料金・品質順位の保証ではありません。公式既定が変われば`default`と`standard`は異なるmodelになり得ます。静的モードではselectorを自動生成しません。

- 起動時と定期的に取得し、モデル名・一覧の編集や再起動なしで新しい独立requestへ反映します。refreshは5–3600秒、最大ageは5–86400秒で、refresh間隔以上が必要です。
- 世代aliasは先頭`gpt-`を除き、`.`を`-`へ変え、`codex-`を付けます。例：`gpt-6.1-sol`→`codex-6-1-sol`。他のモデル名は全体をstemとして残します。
- 不正identifier、alias衝突、モデル重複、既定なし／複数、未完paginationは更新全体を拒否します。モデルrecordは最大128件、paginationは最大8ページです。
- 既存TOMLは起動時に読む固定aliasの入力設定です。自動生成一覧ではありません。対象modelが公開カタログに存在する間だけ有効で、別modelへ勝手に付け替えません。自動モードでは`default`・`standard`・`advanced`・`fast`を予約し、TOMLでの上書きは起動時に拒否します。
- 一つの不変snapshotで一覧公開と受付を揃え、requestはcredentials待機より前に実model・署名authorityを確定します。更新失敗で直前正常snapshotの寿命は延長しません。未取得／期限切れではreadiness・一覧・生成を503にし、静的設定へfallbackしません。cacheはmemory-onlyで、再起動時は新たな取得が必要です。
- 4 selectorのreasoning・compaction・tool継続は、既存の再送ID内に入る署名付き`cobr_t1_` receiptで開始時modelへ固定します。実model名・route署名鍵は公開せず、既存のID/state/assistant意味結合も検証します。model撤去・改ざん・route混在・alias跨ぎ・receipt剥離はcredentials/upstream呼出し前に拒否します。
- 認証済み継続情報のない平文履歴だけでは元modelを証明できないため、新しい独立requestとして扱います。再起動を跨ぐ継続には同じcontinuation keyを保持してください。会話履歴をserverに保存する機能は追加しません。

カタログへの掲載は全bridge機能との互換性保証ではありません。従来の厳格なrequest/SSE検証・各種上限は維持し、未知のprovider挙動の透過や別modelでの自動再送はしません。

生成されるuser unitには、意図的に`EnvironmentFile=`もsecretも含めません。他のsecret以外のoverridesには、user-unit drop-inを使用してください。

```ini
[Service]
Environment=CODEX_BRIDGE_PORT=8646
```

`systemctl --user edit codex-openai-bridge.service` で作成し、その後 `systemctl --user daemon-reload` を実行して service を再起動します。

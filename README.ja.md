# codex-openai-bridge

[English (canonical)](README.md) | **日本語**

> 英語版が正本（canonical）です。日本語訳と異なる場合は英語版を優先します。

[![CI](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/itmst71/codex-openai-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/downloads/)

既存の**OpenAI公式Codex CLIへのChatGPTログイン**を、OpenAI互換クライアントから利用するためのローカルブリッジです。
Chat Completionsと制限付きのResponses APIをCodex backendへ変換します。
自然言語の翻訳サービスでもエージェントでもなく、プロトコルの変換器です。履歴の管理とツールの実行は接続するアプリが行います。

`Your application → 127.0.0.1:8646 → Codex backend`

**個人・単一ユーザー・ローカルでの実験用途に限定します。** アクセスを共有したり、
team・hosted・public・commercial・CI inference serviceとして提供したりしないでください。
共有サービスやproduction automationにはOpenAI公式APIを使用してください。[利用条件と範囲](docs/service-terms.ja.md)。

テキスト生成・ストリーミング・構造化出力・検証付きツール継続に対応します。
**OpenAI API全体の置き換えではありません。** embeddings、画像入力、hosted tools、
`previous_response_id`、サーバー側の会話保存には対応しません。[正確なAPI仕様](docs/api.ja.md)。

## 前提条件

- Linux、Python **3.12**、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Git、curl。
- OpenAI公式Codex CLIをインストールし、**file** credential storageを使ってChatGPTにログイン済みであること。
  以下の手順を始める前に[CLIの準備と認証情報の権限](docs/setup.ja.md)を確認してください。
- 要求するCodexモデルを利用可能なアカウント。OpenAI Developer API keyでは代用できません。
- 既存環境の更新には、キー生成ではなく[通常更新の手順](docs/operations.ja.md)を使ってください。

## クイックスタート

CLIログインを所有するUnixユーザーで、ブリッジのホスト上で実行します。
この例は明示的にdiscoveryを有効にします。製品の既定値はstatic modeのままです。

### 1. インストール

```bash
git clone https://github.com/itmst71/codex-openai-bridge.git
cd codex-openai-bridge
uv sync --locked
```

### 2. キーを初回だけ生成

**新規導入時だけ実行してください。** 既存file・symlinkがあれば停止します。更新時にキーを上書きしてはいけません。
クライアント用トークンとサーバー用署名キーを別々に作成し、値は表示しません。

```bash
uv run python - <<'PY'
from pathlib import Path
import os
import secrets

config = Path.home() / ".config"
config.mkdir(mode=0o700, exist_ok=True)
root = config / "codex-openai-bridge"
root.mkdir(mode=0o700, exist_ok=True)
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    if os.fstat(directory).st_uid != os.geteuid():
        raise SystemExit("Credential directory must belong to the current user.")
    names = ("client-token", "continuation-key")
    for name in names:
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SystemExit(
                "Existing credentials found; preserve them. Do not regenerate on upgrade."
            )
    values = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    if values[0] == values[1]:
        raise SystemExit("Generated identical keys; nothing written. Try again.")
    os.fchmod(directory, 0o700)
    for name, value in zip(names, values, strict=True):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
finally:
    os.close(directory)
PY
```

### 3. 前景で起動

```bash
CODEX_BRIDGE_CODEX_PATH="$(command -v codex)" \
CODEX_BRIDGE_MODEL_DISCOVERY=codex \
  uv run python -m codex_openai_bridge serve
```

このターミナルを開いたままにし、別のターミナルでreadinessを確認します。

```bash
curl -q --noproxy '*' --fail --silent --show-error http://127.0.0.1:8646/readyz
```

`/healthz`はプロセスの生存確認だけです。有効なカタログを取得するまで、または最後の正常なsnapshotが
期限切れになると、discoveryと生成は503になります。readinessが失敗する場合はCLIログインとCLIの指定pathを
確認してください。[設定とdiscoveryの詳細](docs/models.ja.md)。

### 4. 応答を生成

2つ目のターミナルで実行します。

```bash
IFS= read -r bridge_token < "$HOME/.config/codex-openai-bridge/client-token"
curl -q --noproxy '*' --retry 0 --connect-timeout 5 --max-time 245 \
  --fail --silent --show-error \
  -H "Authorization: Bearer $bridge_token" \
  -H 'Content-Type: application/json' \
  --data '{"model":"default","messages":[{"role":"user","content":"Reply briefly."}]}' \
  http://127.0.0.1:8646/v1/chat/completions
unset bridge_token
```

成功時は`choices[0].message.content`に応答が入ります。実際にCodexへ要求するため、アカウントの利用枠を消費します。
コマンドはトークンを表示しませんが、同一UIDのプロセスによる引数の参照は脅威モデルの対象外です。
[systemd user serviceへ切り替える](docs/operations.ja.md)前に、Ctrl-Cで前景のブリッジを停止してください。

## アプリから接続

| 設定 | 値 |
| --- | --- |
| Base URL | `http://127.0.0.1:8646/v1` |
| Client API key / bearer token | `~/.config/codex-openai-bridge/client-token`の内容 |
| discoveryを使うクイックスタートのmodel | `default` |
| SDKの自動retry | 重複生成を避けるため無効化（`max_retries=0`） |

次の3つは別の権限です。

| 権限 | 使用する主体 | 使用してはいけない用途 |
| --- | --- | --- |
| Codex CLIログイン（`auth.json`） | 公式CLIのcredential helper | クライアントのAPI keyや公開ログ |
| `client-token` | アプリの`api_key` / bearer欄 | Codexログインや継続への署名 |
| `continuation-key` | ブリッジ内部だけ | あらゆるconsumer設定 |

いずれも非公開で保管してください。アプリの`api_key`欄には、OpenAI API keyではなく**ブリッジのトークン**を渡します。
[実行可能なOpenAI Python SDK例](docs/integrations/openai-python.ja.md)。

## モデルを選択

| 公開alias | 利用可能な条件 | 選択規則 |
| --- | --- | --- |
| `codex` | static mode。discoveryでは固定先が公開されている間だけ | 固定の互換route。自動で最新版にはならない |
| `default` | discovery | 公式の公開`isDefault`モデル |
| `standard` | discoveryでSolが存在するとき | Sol系列の最新canonical世代 |
| `advanced` | discoveryでAstraが存在するとき | Astra系列の最新canonical世代 |
| `fast` | discoveryでLunaが存在するとき | Luna系列の最新canonical世代 |

クイックスタートは`CODEX_BRIDGE_MODEL_DISCOVERY=codex`を有効にします。指定しなければ既定はstatic modeで、
互換aliasは`codex`です。実際に利用できる公開aliasは`/v1/models`で確認してください。
系列名は選択方針であり、速度・価格・品質の実測保証ではありません。
tool・reasoning・compactionの継続中は同じaliasを使ってください。discoveryの継続は開始時のモデルへ固定し、
モデルが撤去された場合は別モデルへ黙って切り替えず拒否します。
[static map・discovery設定・継続の規則](docs/models.ja.md)。

## コンシューマー互換性

検証段階は区別してください。**contract**は実パッケージとfixture、**live**は実際のCodexを含む検証、
**operational**は記載した範囲内での反復的な実利用です。製品全体の対応を保証する表ではありません。
版数・必要なadapter・未検証範囲は[詳細な検証表](docs/integrations.ja.md)に記載しています。

| コンシューマー | 検証段階 | 主な条件・手順 |
| --- | --- | --- |
| OpenAI Python SDK | Live | [自動retryを無効化](docs/integrations/openai-python.ja.md) |
| Unhush Custom Formatting | Operational・範囲限定・user-reported | [文字起こし後の整形のみ。STTは別backend](docs/integrations/unhush.ja.md) |
| Honcho | Operational | [テキスト生成のみ。embeddingsは別backend](docs/integrations/honcho.ja.md) |
| LangChain | Operational・範囲限定 | [明示的なtransport。並列処理・永続化の検証範囲に注意](docs/integrations/langchain.ja.md) |
| OpenAI Agents SDK | Live・設定が必要 | [Chat Completions model。tracing無効](docs/integrations/agents.ja.md) |
| AutoGen | Contract・adapterが必要 | [明示的なcapabilitiesと条件付きtools adapter](docs/integrations/autogen.ja.md) |
| Aider | Contract・用途限定 | [単発のwhole-file editのみ](docs/integrations/aider.ja.md) |
| Cline CLI | Live・用途限定 | [headless・3つのlocal toolによる編集のみ](docs/integrations/cline.ja.md) |
| Continue core | Contract・componentのみ | [Chat/Edit provider。IDEやCLI全体ではない](docs/integrations/continue.ja.md) |

## 詳細資料

- [導入・ログイン・認証境界・セキュリティ上の前提](docs/setup.ja.md)
- [API対応範囲・拒否するfield・SSEと継続の契約](docs/api.ja.md)
- [モデル・discovery・環境変数](docs/models.ja.md)
- [systemd・通常更新・rollback・旧版移行・明示的なlive test](docs/operations.ja.md)
- [consumerの版数と正確な検証範囲](docs/integrations.ja.md)
- [想定用途とサービス利用条件](docs/service-terms.ja.md)

## 貢献・セキュリティ・ライセンス

[貢献ガイド](CONTRIBUTING.ja.md)にissue-firstの手順と検証gateを記載しています。
脆弱性は[SECURITY.md](SECURITY.md)に従って非公開で報告し、credential・実prompt・tool引数・
生のupstream responseを公開報告に添付しないでください。
[MIT License](LICENSE)はsource codeに適用され、OpenAIサービスの利用権を与えるものではありません。

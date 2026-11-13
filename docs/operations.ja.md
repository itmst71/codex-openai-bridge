# 常駐運用・更新・移行

[English (canonical)](operations.md) | **日本語**

英語版を正本（canonical）とします。 [README](../README.ja.md)

## systemd user service

host には user manager が必要です。logout/boot 後も動作を継続するには、さらに linger が必要です（`loginctl show-user "$USER" -p Linger`）。linger の有効化には administrator が必要な場合があります。

任意のabsolute checkout pathからlocked environmentを同期し、実行fileとCodex pathを固定した
machine-local unitを生成します。

```bash
cd /path/to/codex-openai-bridge
uv sync --locked
uv run python scripts/install_user_service.py \
  --checkout "$PWD" \
  --codex-path "$(command -v codex)"
systemctl --user daemon-reload
systemctl --user enable codex-openai-bridge.service
```

installerは`deploy/systemd/codex-openai-bridge.service.in`をrenderし、生成unitを検証してから
atomicにinstallします。serviceのstart、stop、enable、reloadを実行しません。既存unitは`--force`なしで
置換しません。生成unitにはmachine-local absolute pathが含まれますが、repository templateにはcheckout
locationを含めません。
`--force`では単一のatomic exchangeで置換し、旧unitを表示されたhidden filenameで保持します。失敗時も、
同時にpathnameが変更された可能性があるtemporary／unexpected entryを自動削除せず保持します。表示された
fileの内容を確認してから手動で削除してください。
`--force`成功後も、表示された旧unit fileを確認し、rollbackが不要になった場合にだけ手動で削除してください。
失敗時は通常、到達可能なexact absolute pathを表示します。Concurrentなparent-directory renameによりabsolute
pathを安全に特定できない場合は、stale pathを作らず
`opened-parent(dev=...,ino=...)/<basename> (absolute location unavailable)`を表示します。手動削除の前に、
表示されたdevice/inodeでdirectoryを特定し、basenameの内容を確認してください。

起動前に、手動で起動したブリッジがあれば停止し、port 8646 が空いていることを確認します。

```bash
ss -ltnp '( sport = :8646 )'
systemctl --user start codex-openai-bridge.service
```

実際の unit、process、listener、health、journal を確認します。

```bash
systemctl --user is-enabled codex-openai-bridge.service
systemctl --user is-active codex-openai-bridge.service
systemctl --user show codex-openai-bridge.service \
  -p MainPID -p NRestarts -p ActiveState -p SubState
ss -ltnp '( sport = :8646 )'
curl --fail --silent --show-error http://127.0.0.1:8646/healthz
curl --fail --silent --show-error http://127.0.0.1:8646/readyz
journalctl --user -u codex-openai-bridge.service --no-pager -n 30
```

設定した port を listen する正規のブリッジ process は 1 つだけにしてください。

## 通常更新

upgrade前に現在install済みの検証済みunitをcheckout外へ保存し、稼働中のrevisionを記録してください。

**既存の2つのキーを保持してください。** 通常更新で`client-token`や`continuation-key`を生成・上書きしてはいけません。
どちらもowner管理のregular file、mode `0600`、link数1とし、互いに異なる値であることを維持します。
CLIログインとキーはGit rollbackの対象外です。署名方式に互換性のある更新は既存キーを保持し、
互換性を破る旧版からの移行だけ、下の専用手順で扱います。

作業前にactiveな要求を完了し、関連するsocketやproxyがあれば停止して自動再起動を防いでください。
listenerが残っていれば続行せず所有者を確認します。停止後、以下の各commandが成功した場合だけ次へ進みます。
起動前に[貢献ガイドのquality gate](../CONTRIBUTING.ja.md)を満たしてください。
rollback directoryは新規の一意なpathで、既存backupを上書きしません。

```bash
cd /path/to/codex-openai-bridge
backup_dir="$(mktemp -d "$HOME/.config/codex-openai-bridge/rollback.XXXXXXXX")"
install -m 600 "$HOME/.config/systemd/user/codex-openai-bridge.service" "$backup_dir/service"
git rev-parse HEAD > "$backup_dir/revision"
printf 'Keep this rollback directory: %s\n' "$backup_dir"
systemctl --user stop codex-openai-bridge.service
ss -ltnp '( sport = :8646 )'
git switch --detach <reviewed-revision>
uv sync --locked
uv run pytest -q
uv run mypy src tests scripts
uv run ruff check .
uv run ruff format --check .
uv run python scripts/verify_systemd_unit.py
uv run python scripts/install_user_service.py \
  --checkout "$PWD" \
  --codex-path "$(command -v codex)" \
  --force
systemctl --user daemon-reload
systemctl --user start codex-openai-bridge.service
curl -q --noproxy '*' --fail --silent --show-error http://127.0.0.1:8646/readyz
```

起動後は上記のunit・process・listener・health・journalに加えて実際の生成を確認します。
keyの値を表示してはいけません。

## ロールバック

対象revisionが`install_user_service.py`より古い場合は、存在しないinstallerを呼び出す代わりに保存済みunitを復元します。

rollback前に新しいrevisionで作成したactive chainを完了するか、rollback後はfresh chainで再開してください。
署名authorityが変わる境界を越えて継続できるとは限りません。記録したrevisionと対応するlockfileへ戻し、
source・lockfile・virtual environment・unitを異なるrevisionから混在させないでください。
`backup_dir`には上の作業で保存したdirectoryを指定します。

```bash
cd /path/to/codex-openai-bridge
systemctl --user stop codex-openai-bridge.service
git switch --detach <previously-recorded-revision>
uv sync --locked
if test -f scripts/install_user_service.py; then
  uv run python scripts/install_user_service.py \
    --checkout "$PWD" \
    --codex-path "$(command -v codex)" \
    --force
else
  install -m 644 "$backup_dir/service" \
    "$HOME/.config/systemd/user/codex-openai-bridge.service"
fi
systemctl --user daemon-reload
systemctl --user start codex-openai-bridge.service
```

## 旧署名方式からの移行

この節は、client tokenで署名した`cobr_r2_`を生成する旧no-map版から移行するときだけ適用します。
upgrade前にactiveなtoolまたはreasoning chainをすべて完了してください。完了できなければ移行後はfresh chainを開始します。
review済みrevisionを起動する前に`continuation-key`を生成して検証するのは、server-only keyを持たない旧環境だけです。
既存のkeyを置換せず、必要なfileだけをexclusive createし、mode `0600`・link数1・`client-token`と異なる値を検証します。
通常更新用のキー再生成手順ではありません。旧client-mintable authorityの継続は受理しません。

## 明示的に有効化する live tests

通常の tests は fake loopback upstreams を使用し、実際の credentials は使用しません。明示的に有効化しない限り、live tests は動作しません。

別途起動し、検証済みのブリッジを使用する場合は次のとおりです。

```bash
export RUN_LIVE_CODEX=1
export CODEX_BRIDGE_LIVE_BASE_URL=http://127.0.0.1:8646/v1
IFS= read -r CODEX_BRIDGE_LIVE_API_KEY \
  < "$HOME/.config/codex-openai-bridge/client-token"
export CODEX_BRIDGE_LIVE_API_KEY
uv run pytest tests/live/test_live_codex.py -q
unset RUN_LIVE_CODEX CODEX_BRIDGE_LIVE_BASE_URL CODEX_BRIDGE_LIVE_API_KEY
```

より広範な SDK smoke script も、明示的に有効化した場合のみ実行されます。明示的な base URL と token environment variable がなければ接続を拒否します。

```bash
export CODEX_BRIDGE_BASE_URL=http://127.0.0.1:8646/v1
IFS= read -r CODEX_BRIDGE_CLIENT_TOKEN \
  < "$HOME/.config/codex-openai-bridge/client-token"
export CODEX_BRIDGE_CLIENT_TOKEN
uv run python scripts/smoke_openai_sdk.py
unset CODEX_BRIDGE_BASE_URL CODEX_BRIDGE_CLIENT_TOKEN
```

credential values を bug reports、test logs、shell transcripts、review prompts に含めないでください。

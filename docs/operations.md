# Service operation, upgrades, and migration

**English (canonical)** | [日本語](operations.ja.md)

English is canonical if translations differ. [README](../README.md)

## systemd user service

The host must provide a user manager. Logout/boot persistence additionally requires linger (`loginctl show-user "$USER" -p Linger`). Enabling linger may require an administrator.

From any absolute checkout path, synchronize the locked environment and generate a machine-local
unit with exact executable and Codex paths:

```bash
cd /path/to/codex-openai-bridge
uv sync --locked
uv run python scripts/install_user_service.py \
  --checkout "$PWD" \
  --codex-path "$(command -v codex)"
systemctl --user daemon-reload
systemctl --user enable codex-openai-bridge.service
```

The installer renders `deploy/systemd/codex-openai-bridge.service.in`, verifies the generated unit,
and atomically installs it. It does not start, stop, enable, or reload the service. It refuses to
replace an existing unit unless `--force` is supplied. The generated unit intentionally contains
machine-local absolute paths; the repository template does not contain a checkout location.
With `--force`, replacement uses one atomic exchange and retains the previous unit under the
reported hidden filename. Failed installations likewise retain any temporary or unexpected entry
instead of unlinking a pathname that may have changed concurrently; inspect the reported file and
remove it manually only after confirming its contents.
After a successful `--force`, inspect the reported previous-unit file and remove it manually only
when rollback is no longer required. Failure output normally reports an exact reachable absolute
path. If a concurrent parent-directory rename prevents that, it reports
`opened-parent(dev=...,ino=...)/<basename> (absolute location unavailable)` instead of inventing a
stale path; locate the directory by the reported device/inode and inspect the basename before any
manual removal.

Before starting, stop any manually launched bridge and prove port 8646 is free:

```bash
ss -ltnp '( sport = :8646 )'
systemctl --user start codex-openai-bridge.service
```

Verify the actual unit, process, listener, health, and journal:

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

Only one authoritative bridge process should listen on the configured port.

## Ordinary upgrades

Before upgrading, preserve the currently installed verified unit outside the checkout and record the deployed revision.

**Preserve both existing keys.** Never generate or overwrite `client-token` or `continuation-key`
as part of an ordinary upgrade. Keep owner-controlled regular files at mode `0600`, with one link,
and ensure the signing key differs from `client-token`. CLI login and keys are not part of a Git rollback.
Compatible signing-authority upgrades retain existing keys; an older incompatible release needs
only the specific legacy migration below.

Finish active requests first. Stop related sockets/proxies, if any, to prevent reactivation.
If a listener remains, stop and identify its owner rather than proceeding. After stopping, continue
only when each command succeeds. Meet the [contribution quality gates](../CONTRIBUTING.md) before
starting. The unique rollback directory preserves earlier backups instead of overwriting them.

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

After starting, verify unit/process/listener/health/journal as above and perform a real generation.
Do not print either key.

## Rollback

If the target revision predates `install_user_service.py`, restore the preserved unit instead of invoking a missing installer.

Before rollback, finish active chains created by the newer revision or start a fresh chain afterward.
Signed state is not necessarily portable across a signing-authority boundary. Restore the recorded
revision and matching lockfile; do not mix source, lockfile, environment, or unit definitions from
different revisions. Set `backup_dir` to the directory saved above.

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

## Legacy signing-authority migration

This section applies only when migrating from an older no-map release that produced `cobr_r2_`
Chat reasoning details signed with the client token. Before upgrading, finish every active tool or reasoning chain.
If it cannot finish, start a fresh chain after the cutover.
Generate and verify `continuation-key` before starting the reviewed revision only when that legacy
installation has no server-only key. Do not replace an existing key: exclusively create only the
missing signing-key file, verify mode `0600`, one link, and a value that differs from `client-token`.
This is not a key-regeneration step for ordinary upgrades. The client-mintable old authority is rejected.

## Opt-in live tests

Normal tests use fake loopback upstreams and no real credentials. Live tests are inert unless explicitly enabled.

With a separately started, verified bridge:

```bash
export RUN_LIVE_CODEX=1
export CODEX_BRIDGE_LIVE_BASE_URL=http://127.0.0.1:8646/v1
IFS= read -r CODEX_BRIDGE_LIVE_API_KEY \
  < "$HOME/.config/codex-openai-bridge/client-token"
export CODEX_BRIDGE_LIVE_API_KEY
uv run pytest tests/live/test_live_codex.py -q
unset RUN_LIVE_CODEX CODEX_BRIDGE_LIVE_BASE_URL CODEX_BRIDGE_LIVE_API_KEY
```

The broader SDK smoke script is also opt-in and refuses to connect without an explicit base URL and token environment variable:

```bash
export CODEX_BRIDGE_BASE_URL=http://127.0.0.1:8646/v1
IFS= read -r CODEX_BRIDGE_CLIENT_TOKEN \
  < "$HOME/.config/codex-openai-bridge/client-token"
export CODEX_BRIDGE_CLIENT_TOKEN
uv run python scripts/smoke_openai_sdk.py
unset CODEX_BRIDGE_BASE_URL CODEX_BRIDGE_CLIENT_TOKEN
```

Never include credential values in bug reports, test logs, shell transcripts, or review prompts.

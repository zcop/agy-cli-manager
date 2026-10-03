# Changelog

## Unreleased

- parse and persist separate Gemini and Claude/GPT-OSS quota families
- show five-hour and weekly quota for both model families in the TUI
- add family-aware failover selection with `--family gemini|other`
- treat either five-hour or weekly exhaustion as blocking for the requested family
- support independent Gemini and Other-family failover thresholds
- keep quota cooldowns scoped to the depleted family instead of disabling the whole account
- expose persisted model-family usage in status snapshots used by the TUI and JSON API
- add `resolve-route` for configurable account-first, family-first, or strict-family routing decisions
- add optional Linux Secret Service support for keyring-only `agy` credentials
- add atomic keyring/file/runtime activation with rollback before active state is published
- add `credential-status`, `credential-backend`, and `capture-active` commands
- isolate named `agy` probes from the global Linux keyring

## v0.2.2 - 2026-09-21

- add `agy-cli-manager watch` to tail Antigravity CLI logs and fail over on `Individual quota reached`
- poll those logs from the dashboard in auto mode and record `trigger=log-watch`
- persist log cursors in `log-watch.json` so historical quota errors are not replayed
- replace the watch diagram with a simple 3-step `gpt-image-2` image in `docs/quota-log-watch.png`
- use `msvcrt` file locking on Windows so the manager can import without `fcntl`
- arm a log-triggered switch only once per producing `agy` session so leftover quota lines cannot burn standby accounts
- initialize existing logs at EOF; only files created after watcher start are read from offset 0
- lock and atomically replace `log-watch.json` so dashboard and `watch` cannot clobber cursors
- add `agy-cli-manager ack-restart` (dashboard `Y`) to clear `restart_required` after `agy` is restarted
- persist an `initialized` marker so the first log created after an empty start is read from offset 0

### Fixes

- reject account names that escape the accounts directory or resolve through an account symlink
- write manager state atomically and keep status reads from overwriting concurrent switches
- preserve an explicitly cleared live directory across state reloads
- finish failover cleanly when every standby account is missing usable authentication
- allow a new `agy` session to fail over immediately, even within the previous switch's deduplication window
- run named model and identity probes in the selected account's own home without changing the shared runtime
- refresh quota against the selected account's saved profile so a concurrent switch cannot copy another account's token into it
- run interactive login in a temporary home so failed login leaves the live account intact and adding a standby does not silently activate it

## v0.2.1 - 2026-07-15

- preserve the explicit profile name supplied during login

## v0.2.0 - 2026-07-01

- add account model discovery via Python API and `agy-cli-manager models --json`
- add `refresh-due` for non-interactive due-account usage refresh
- improve identity detection with local Antigravity log parsing
- expand README with first-run setup, API usage, and a sanitized dashboard screenshot
- add MIT license

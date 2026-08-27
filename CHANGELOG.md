# Changelog

## Unreleased

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

## v0.2.1 - 2026-07-15

- preserve the explicit profile name supplied during login

## v0.2.0 - 2026-07-01

- add account model discovery via Python API and `agy-cli-manager models --json`
- add `refresh-due` for non-interactive due-account usage refresh
- improve identity detection with local Antigravity log parsing
- expand README with first-run setup, API usage, and a sanitized dashboard screenshot
- add MIT license

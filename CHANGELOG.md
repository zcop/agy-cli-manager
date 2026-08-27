# Changelog

## Unreleased

- add `agy-cli-manager watch` to tail Antigravity CLI logs and fail over on `Individual quota reached`
- poll those logs from the dashboard in auto mode and record `trigger=log-watch`
- persist log cursors in `log-watch.json` so historical quota errors are not replayed
- use `msvcrt` file locking on Windows so the manager can import without `fcntl`

## v0.2.1 - 2026-07-15

- preserve the explicit profile name supplied during login

## v0.2.0 - 2026-07-01

- add account model discovery via Python API and `agy-cli-manager models --json`
- add `refresh-due` for non-interactive due-account usage refresh
- improve identity detection with local Antigravity log parsing
- expand README with first-run setup, API usage, and a sanitized dashboard screenshot
- add MIT license

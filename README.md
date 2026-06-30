# agy-cli-manager

`agy-cli-manager` is a small manager for running one active Antigravity CLI account with standby accounts available for failover.

Integration model:

- `agy-cli-manager` is application-agnostic.
- external apps can use it as a Python library or as a CLI with JSON output.
- Telegram bot integration is only one possible caller, not a built-in dependency.

Planned scope:

- store multiple account profiles safely
- keep one active account at a time
- switch to the next standby account on quota or auth failure
- clear cached CLI token state after switching
- expose simple CLI commands for status, switch, and health

Current phase 1 includes:

- manager root layout
- account profile import from an existing directory
- active account state
- detected account identity from the saved profile when available
- `switch` and `switch-next`
- token-cache cleanup in the runtime directory
- enable/disable account flags

Phase 2 adds:

- global file lock for safe switching
- cooldown state for exhausted or bad accounts
- failure counters and last-error tracking
- `mark-bad` and `clear-bad`

Phase 4 adds:

- `import-current` to bootstrap from an existing live `.gemini`
- `login <name>` to run isolated interactive `agy` OAuth and save the resulting profile
- first successful profile auto-activates if no active account exists

Directory layout:

```text
~/.agy-cli-manager/
├── accounts/
│   └── <account-name>/
│       ├── .gemini/
│       │   └── ...
│       ├── .config/
│       │   └── ...
│       ├── .cache/
│       │   └── ...
│       └── .local/
│           └── ...
├── runtime/
│   ├── .gemini/
│   ├── .config/
│   ├── .cache/
│   └── .local/
└── state.json
```

Optional integration:

- `live_dir` can point at a real Antigravity/Gemini CLI home such as `~/.gemini`
- when set, switches sync the managed CLI home snapshot into that runtime home and clear token cache there too

Commands:

```bash
agy-cli-manager
agy-cli-manager dashboard
agy-cli-manager menu
agy-cli-manager init
agy-cli-manager list
agy-cli-manager current
agy-cli-manager status
agy-cli-manager status --json
agy-cli-manager whoami
agy-cli-manager whoami account1 --refresh
agy-cli-manager whoami account1 --probe-usage --agy-binary /path/to/agy
agy-cli-manager add account1 /path/to/source
agy-cli-manager import-current account1
agy-cli-manager import-current account1 /path/to/.gemini
agy-cli-manager login
agy-cli-manager login account1 --agy-binary /path/to/agy
agy-cli-manager activate account1
agy-cli-manager switch account1
agy-cli-manager rotate
agy-cli-manager switch-next
agy-cli-manager disable account1
agy-cli-manager enable account1
agy-cli-manager mark-bad account1 --reason quota --cooldown-minutes 60
agy-cli-manager clear-bad account1
agy-cli-manager set-live-dir ~/.gemini
agy-cli-manager apply-active
agy-cli-manager rotate-after-failure --reason quota --cooldown-minutes 60 --json
```

`add` accepts either:

- a directory that is already a `.gemini` profile root
- or a parent directory containing `.gemini/`

Notes:

- running `agy-cli-manager` with no subcommand opens the full-screen dashboard
- `dashboard` is a TTY-only full-screen view with a fast local-only UI refresh and manual account actions
- `list`, `current`, `activate`, and `rotate` are convenience commands for standalone use; they map to the same manager state as the lower-level commands.
- `agy-cli-manager login` prompts for the account name if you do not pass one
- `switch-next` skips accounts in cooldown.
- `mark-bad` clears the active pointer if that account was active.
- state and switching are protected by a single lock file so a caller can safely trigger failover from another process.
- `set-live-dir` lets the manager drive a real CLI home in addition to its own internal `runtime/`.
- the manager snapshots the managed CLI home paths (`.gemini`, `.config`, `.cache`, `.local`) instead of assuming a single token file is enough.
- it supports both Gemini-style root auth files and Antigravity-style `antigravity-cli/antigravity-oauth-token` auth storage.
- `login` hands the terminal directly to a real `agy` session in the configured runtime home; complete onboarding/login there, exit `agy`, and the manager then saves the captured profile snapshot.
- `login` stores the profile under the detected account name when available, not just the typed label.
- if that detected account already exists, `login` warns and asks whether to overwrite the saved profile.
- `whoami` reports the detected signed-in account name from profile metadata, and `--probe-usage` can additionally run `agy -p /usage` against that profile as a live check.
- the manager intentionally does not use scripted PTY startup probing for `agy`; profile switching is filesystem-based and runtime health should come from real request success/failure in the caller.
- `rotate-after-failure` is the public failover operation for external apps: mark the current active account bad, optionally put it in cooldown, then switch to the next eligible standby account.
- dashboard keybindings: `Up/Down` or `j/k` move, `n` login, `i` import, `Enter` or `a` activate, `r` rotate, `e` enable/disable, `c` clear bad, `m` mark bad, `s` cycle sort (`added`, `usage`, `countdown`), `u` local refresh, `t` cycle UI refresh (`5s/10s/15s/30s`), `q` quit.

Python usage:

```python
from pathlib import Path

from agy_cli_manager import build_paths, rotate_after_failure, get_status_snapshot

paths = build_paths(Path.home() / ".agy-cli-manager")
snapshot = get_status_snapshot(paths)
result = rotate_after_failure(paths, reason="quota", cooldown_minutes=60)
print(snapshot["active"], "->", result.switched_to)
```

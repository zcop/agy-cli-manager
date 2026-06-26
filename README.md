# agy-cli-manager

`agy-cli-manager` is a small manager for running one active Antigravity CLI account with standby accounts available for failover.

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
- `switch` and `switch-next`
- token-cache cleanup in the runtime directory
- enable/disable account flags

Phase 2 adds:

- global file lock for safe switching
- cooldown state for exhausted or bad accounts
- failure counters and last-error tracking
- `mark-bad` and `clear-bad`

Directory layout:

```text
~/.agy-cli-manager/
├── accounts/
│   └── <account-name>/
│       ├── oauth_creds.json
│       └── google_account_id
├── runtime/
│   ├── oauth_creds.json
│   └── google_account_id
└── state.json
```

Optional integration:

- `live_dir` can point at a real Antigravity/Gemini CLI home such as `~/.gemini`
- when set, switches mirror the active runtime into that directory and clear token cache there too

Commands:

```bash
agy-cli-manager init
agy-cli-manager status
agy-cli-manager add account1 /path/to/source
agy-cli-manager switch account1
agy-cli-manager switch-next
agy-cli-manager disable account1
agy-cli-manager enable account1
agy-cli-manager mark-bad account1 --reason quota --cooldown-minutes 60
agy-cli-manager clear-bad account1
agy-cli-manager set-live-dir ~/.gemini
agy-cli-manager apply-active
```

`source_dir` must contain `oauth_creds.json`. If `google_account_id` exists, it is copied too.

Notes:

- `switch-next` skips accounts in cooldown.
- `mark-bad` clears the active pointer if that account was active.
- state and switching are protected by a single lock file so a caller can safely trigger failover from another process.
- `set-live-dir` lets the manager drive a real CLI home in addition to its own internal `runtime/`.

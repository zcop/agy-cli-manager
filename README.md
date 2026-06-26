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

Commands:

```bash
agy-cli-manager init
agy-cli-manager status
agy-cli-manager add account1 /path/to/source
agy-cli-manager switch account1
agy-cli-manager switch-next
agy-cli-manager disable account1
agy-cli-manager enable account1
```

`source_dir` must contain `oauth_creds.json`. If `google_account_id` exists, it is copied too.

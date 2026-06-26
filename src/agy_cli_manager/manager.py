from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


ACTIVE_RUNTIME_FILES = (
    "oauth_creds.json",
    "google_account_id",
)
TOKEN_CACHE_FILES = (
    "mcp-oauth-tokens-v2.json",
)


@dataclass
class ManagerPaths:
    root: Path
    accounts_dir: Path
    state_file: Path
    runtime_dir: Path


def default_root() -> Path:
    return Path.home() / ".agy-cli-manager"


def build_paths(root: Path) -> ManagerPaths:
    return ManagerPaths(
        root=root,
        accounts_dir=root / "accounts",
        state_file=root / "state.json",
        runtime_dir=root / "runtime",
    )


def ensure_layout(paths: ManagerPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.accounts_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    if not paths.state_file.exists():
        save_state(paths, {"active": None, "accounts": {}})


def load_state(paths: ManagerPaths) -> dict:
    ensure_layout(paths)
    with paths.state_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("active", None)
    data.setdefault("accounts", {})
    return data


def save_state(paths: ManagerPaths, state: dict) -> None:
    with paths.state_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def account_dir(paths: ManagerPaths, name: str) -> Path:
    return paths.accounts_dir / name


def sync_state_from_disk(paths: ManagerPaths, state: dict) -> dict:
    disk_accounts = {p.name for p in paths.accounts_dir.iterdir() if p.is_dir()}
    tracked = state["accounts"]

    for name in sorted(disk_accounts):
        tracked.setdefault(name, {"enabled": True, "status": "standby"})
    for name in list(tracked):
        if name not in disk_accounts:
            tracked.pop(name, None)
            if state.get("active") == name:
                state["active"] = None

    active = state.get("active")
    for name, meta in tracked.items():
        meta["status"] = "active" if name == active else ("standby" if meta.get("enabled", True) else "disabled")
    return state


def add_account(paths: ManagerPaths, name: str, source_dir: Path) -> None:
    if not name.strip():
        raise ValueError("Account name cannot be empty.")
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Source directory does not exist: {source_dir}")

    src_creds = source_dir / "oauth_creds.json"
    if not src_creds.exists():
        raise ValueError(f"Missing oauth_creds.json in {source_dir}")

    target = account_dir(paths, name)
    if target.exists():
        raise ValueError(f"Account already exists: {name}")

    target.mkdir(parents=True, exist_ok=False)
    shutil.copy2(src_creds, target / "oauth_creds.json")
    if (source_dir / "google_account_id").exists():
        shutil.copy2(source_dir / "google_account_id", target / "google_account_id")

    state = load_state(paths)
    state = sync_state_from_disk(paths, state)
    state["accounts"][name] = {"enabled": True, "status": "standby"}
    save_state(paths, state)


def _copy_active_runtime(paths: ManagerPaths, name: str) -> None:
    src = account_dir(paths, name)
    if not src.exists():
        raise ValueError(f"Account not found: {name}")
    if not (src / "oauth_creds.json").exists():
        raise ValueError(f"Account {name} is missing oauth_creds.json")

    for filename in ACTIVE_RUNTIME_FILES:
        src_file = src / filename
        dst_file = paths.runtime_dir / filename
        if src_file.exists():
            shutil.copy2(src_file, dst_file)
        elif dst_file.exists():
            dst_file.unlink()

    for cache_name in TOKEN_CACHE_FILES:
        cache_file = paths.runtime_dir / cache_name
        if cache_file.exists():
            cache_file.unlink()


def switch_account(paths: ManagerPaths, name: str) -> str:
    state = sync_state_from_disk(paths, load_state(paths))
    meta = state["accounts"].get(name)
    if meta is None:
        raise ValueError(f"Account not found: {name}")
    if not meta.get("enabled", True):
        raise ValueError(f"Account is disabled: {name}")

    previous = state.get("active")
    _copy_active_runtime(paths, name)
    state["active"] = name
    state = sync_state_from_disk(paths, state)
    save_state(paths, state)
    return previous or ""


def switch_next(paths: ManagerPaths) -> str:
    state = sync_state_from_disk(paths, load_state(paths))
    enabled_accounts = [name for name, meta in sorted(state["accounts"].items()) if meta.get("enabled", True)]
    if not enabled_accounts:
        raise ValueError("No enabled accounts available.")

    current = state.get("active")
    if current in enabled_accounts:
        idx = (enabled_accounts.index(current) + 1) % len(enabled_accounts)
    else:
        idx = 0
    target = enabled_accounts[idx]
    if len(enabled_accounts) == 1 and current == target:
        raise ValueError("Only one enabled account is available.")
    switch_account(paths, target)
    return target


def set_enabled(paths: ManagerPaths, name: str, enabled: bool) -> None:
    state = sync_state_from_disk(paths, load_state(paths))
    meta = state["accounts"].get(name)
    if meta is None:
        raise ValueError(f"Account not found: {name}")
    meta["enabled"] = enabled
    if not enabled and state.get("active") == name:
        state["active"] = None
    state = sync_state_from_disk(paths, state)
    save_state(paths, state)


def format_status(paths: ManagerPaths) -> str:
    state = sync_state_from_disk(paths, load_state(paths))
    save_state(paths, state)
    lines = [
        f"root: {paths.root}",
        f"runtime: {paths.runtime_dir}",
        f"active: {state.get('active') or '-'}",
        "accounts:",
    ]
    for name, meta in sorted(state["accounts"].items()):
        flag = "enabled" if meta.get("enabled", True) else "disabled"
        lines.append(f"  - {name}: {meta.get('status', 'standby')} ({flag})")
    if not state["accounts"]:
        lines.append("  - none")
    return "\n".join(lines)

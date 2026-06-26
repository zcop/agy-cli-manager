from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager

import fcntl


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
    lock_file: Path


def default_root() -> Path:
    return Path.home() / ".agy-cli-manager"


def build_paths(root: Path) -> ManagerPaths:
    return ManagerPaths(
        root=root,
        accounts_dir=root / "accounts",
        state_file=root / "state.json",
        runtime_dir=root / "runtime",
        lock_file=root / "manager.lock",
    )


def ensure_layout(paths: ManagerPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.accounts_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    if not paths.state_file.exists():
        save_state(paths, {"active": None, "accounts": {}, "live_dir": None})


@contextmanager
def manager_lock(paths: ManagerPaths):
    ensure_layout(paths)
    with paths.lock_file.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()))
            f.flush()
            yield
        finally:
            try:
                f.seek(0)
                f.truncate()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_state(paths: ManagerPaths) -> dict:
    ensure_layout(paths)
    with paths.state_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("active", None)
    data.setdefault("accounts", {})
    data.setdefault("live_dir", None)
    return data


def save_state(paths: ManagerPaths, state: dict) -> None:
    with paths.state_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def account_dir(paths: ManagerPaths, name: str) -> Path:
    return paths.accounts_dir / name


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None


def get_live_dir(state: dict) -> Path | None:
    value = state.get("live_dir")
    if not value:
        return None
    return Path(value)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def sync_state_from_disk(paths: ManagerPaths, state: dict) -> dict:
    disk_accounts = {p.name for p in paths.accounts_dir.iterdir() if p.is_dir()}
    tracked = state["accounts"]

    for name in sorted(disk_accounts):
        tracked.setdefault(
            name,
            {
                "enabled": True,
                "status": "standby",
                "last_error": None,
                "cooldown_until": None,
                "fail_count": 0,
            },
        )
    for name in list(tracked):
        if name not in disk_accounts:
            tracked.pop(name, None)
            if state.get("active") == name:
                state["active"] = None

    active = state.get("active")
    for name, meta in tracked.items():
        cooldown_until = parse_timestamp(meta.get("cooldown_until"))
        in_cooldown = bool(cooldown_until and cooldown_until > utc_now())
        if name == active:
            meta["status"] = "active"
        elif not meta.get("enabled", True):
            meta["status"] = "disabled"
        elif in_cooldown:
            meta["status"] = "cooldown"
        else:
            meta["status"] = "standby"
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

    with manager_lock(paths):
        state = load_state(paths)
        state = sync_state_from_disk(paths, state)
        state["accounts"][name] = {
            "enabled": True,
            "status": "standby",
            "last_error": None,
            "cooldown_until": None,
            "fail_count": 0,
        }
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


def _sync_runtime_to_live_dir(paths: ManagerPaths, state: dict) -> None:
    live_dir = get_live_dir(state)
    if live_dir is None:
        return
    live_dir.mkdir(parents=True, exist_ok=True)
    for filename in ACTIVE_RUNTIME_FILES:
        src_file = paths.runtime_dir / filename
        dst_file = live_dir / filename
        if src_file.exists():
            shutil.copy2(src_file, dst_file)
        elif dst_file.exists():
            dst_file.unlink()
    for cache_name in TOKEN_CACHE_FILES:
        cache_file = live_dir / cache_name
        if cache_file.exists():
            cache_file.unlink()


def switch_account(paths: ManagerPaths, name: str) -> str:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(name)
        if meta is None:
            raise ValueError(f"Account not found: {name}")
        if not meta.get("enabled", True):
            raise ValueError(f"Account is disabled: {name}")
        cooldown_until = parse_timestamp(meta.get("cooldown_until"))
        if cooldown_until and cooldown_until > utc_now():
            raise ValueError(f"Account is in cooldown until {cooldown_until.isoformat()}: {name}")

        previous = state.get("active")
        _copy_active_runtime(paths, name)
        state["active"] = name
        state = sync_state_from_disk(paths, state)
        _sync_runtime_to_live_dir(paths, state)
        save_state(paths, state)
        return previous or ""


def switch_next(paths: ManagerPaths) -> str:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        candidates = [
            name
            for name, meta in sorted(state["accounts"].items())
            if meta.get("enabled", True) and meta.get("status") != "cooldown"
        ]
        if not candidates:
            raise ValueError("No enabled non-cooldown accounts available.")

        current = state.get("active")
        if current in candidates:
            idx = (candidates.index(current) + 1) % len(candidates)
        else:
            idx = 0
        target = candidates[idx]
        if len(candidates) == 1 and current == target:
            raise ValueError("Only one eligible account is available.")
        _copy_active_runtime(paths, target)
        state["active"] = target
        state = sync_state_from_disk(paths, state)
        _sync_runtime_to_live_dir(paths, state)
        save_state(paths, state)
        return target


def set_enabled(paths: ManagerPaths, name: str, enabled: bool) -> None:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(name)
        if meta is None:
            raise ValueError(f"Account not found: {name}")
        meta["enabled"] = enabled
        if not enabled and state.get("active") == name:
            state["active"] = None
        state = sync_state_from_disk(paths, state)
        save_state(paths, state)


def mark_bad(paths: ManagerPaths, name: str, reason: str, cooldown_minutes: int) -> None:
    if cooldown_minutes < 0:
        raise ValueError("Cooldown minutes must be non-negative.")
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(name)
        if meta is None:
            raise ValueError(f"Account not found: {name}")
        meta["last_error"] = reason
        meta["fail_count"] = int(meta.get("fail_count", 0)) + 1
        if cooldown_minutes > 0:
            meta["cooldown_until"] = (utc_now() + timedelta(minutes=cooldown_minutes)).isoformat()
        else:
            meta["cooldown_until"] = None
        if state.get("active") == name:
            state["active"] = None
        state = sync_state_from_disk(paths, state)
        save_state(paths, state)


def clear_bad(paths: ManagerPaths, name: str) -> None:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(name)
        if meta is None:
            raise ValueError(f"Account not found: {name}")
        meta["last_error"] = None
        meta["cooldown_until"] = None
        state = sync_state_from_disk(paths, state)
        save_state(paths, state)


def set_live_dir(paths: ManagerPaths, live_dir: Path | None) -> None:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        state["live_dir"] = str(live_dir.resolve()) if live_dir else None
        if state.get("active"):
            _sync_runtime_to_live_dir(paths, state)
        save_state(paths, state)


def apply_active(paths: ManagerPaths) -> str:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        active = state.get("active")
        if not active:
            raise ValueError("No active account is set.")
        _copy_active_runtime(paths, active)
        _sync_runtime_to_live_dir(paths, state)
        save_state(paths, state)
        return active


def format_status(paths: ManagerPaths) -> str:
    state = sync_state_from_disk(paths, load_state(paths))
    save_state(paths, state)
    lines = [
        f"root: {paths.root}",
        f"runtime: {paths.runtime_dir}",
        f"lock: {paths.lock_file}",
        f"live_dir: {state.get('live_dir') or '-'}",
        f"active: {state.get('active') or '-'}",
        "accounts:",
    ]
    for name, meta in sorted(state["accounts"].items()):
        flag = "enabled" if meta.get("enabled", True) else "disabled"
        extra = []
        if meta.get("cooldown_until"):
            extra.append(f"cooldown_until={meta['cooldown_until']}")
        if meta.get("fail_count"):
            extra.append(f"fail_count={meta['fail_count']}")
        if meta.get("last_error"):
            extra.append(f"last_error={meta['last_error']}")
        suffix = f" [{' ; '.join(extra)}]" if extra else ""
        lines.append(f"  - {name}: {meta.get('status', 'standby')} ({flag}){suffix}")
    if not state["accounts"]:
        lines.append("  - none")
    return "\n".join(lines)

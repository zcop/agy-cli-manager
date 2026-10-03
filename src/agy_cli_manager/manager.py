from __future__ import annotations

import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from contextlib import contextmanager

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from agy_cli_manager.watch import get_log_watch_snapshot


MANAGED_PROFILE_FILES = (
    "antigravity-cli/antigravity-oauth-token",
)
LOGIN_ARTIFACT_SETS = (
    ("antigravity-cli/antigravity-oauth-token",),
)
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
APPLY_AUTH_EMAIL_PATTERN = re.compile(r"applyAuthResult:\s+email=([^,\s]+)", re.IGNORECASE)
DEFAULT_REFRESH_POLICY_SECONDS = 1800
USAGE_WINDOW_NAMES = ("short", "weekly")
USAGE_FAMILY_NAMES = ("gemini", "other")
DEFAULT_SWITCH_MODE = "auto"
VALID_SWITCH_MODES = ("auto", "manual")
DEFAULT_CREDENTIAL_BACKEND = "auto"
VALID_CREDENTIAL_BACKENDS = ("auto", "file", "secret-service")
DEFAULT_REFRESH_FAILURE_SWITCH_THRESHOLD = 2
DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT = 10.0
DEFAULT_GEMINI_SWITCH_THRESHOLD_PERCENT = DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT
DEFAULT_OTHER_SWITCH_THRESHOLD_PERCENT = DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT
DEFAULT_CANDIDATE_STRATEGY = "balanced"
VALID_CANDIDATE_STRATEGIES = ("balanced", "highest-short", "round-robin")
DEFAULT_FAMILY_FALLBACK_STRATEGY = "same-family-first"
VALID_FAMILY_FALLBACK_STRATEGIES = ("same-family-first", "same-account-first", "strict-family")
DEFAULT_SWITCH_DEDUPE_SECONDS = 15
DEFAULT_SWITCH_HISTORY_LIMIT = 20
CODE_ASSIST_BASE_URL = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_USER_AGENT = "antigravity"
CODE_ASSIST_LOAD_PATH = "/v1internal:loadCodeAssist"
CODE_ASSIST_QUOTA_PATH = "/v1internal:retrieveUserQuota"
CODE_ASSIST_QUOTA_SUMMARY_PATH = "/v1internal:retrieveUserQuotaSummary"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


@dataclass
class ManagerPaths:
    root: Path
    accounts_dir: Path
    state_file: Path
    runtime_dir: Path
    lock_file: Path


@dataclass
class RotationResult:
    previous_active: str | None
    active: str | None
    switched_to: str | None
    marked_bad: bool
    reason: str | None
    cooldown_minutes: int
    outcome: str = "unknown"


@dataclass
class UsageRefreshResult:
    account: str
    source_home: str
    project_id: str | None
    plan_type: str | None
    prompt_credits_available: int | float | None
    prompt_credits_monthly: int | float | None
    short_usage_status: str
    short_usage_value: float | None
    short_reset_at: str | None
    weekly_usage_status: str
    weekly_usage_value: float | None
    weekly_reset_at: str | None
    usage_families: dict
    bucket_count: int


@dataclass
class EnsureActiveResult:
    triggered: bool
    switch_mode: str
    previous_active: str | None
    active: str | None
    switched_to: str | None
    reason: str | None
    cooldown_minutes: int
    required_family: str | None = None


@dataclass
class RouteResult:
    preferred_family: str
    selected_family: str | None
    previous_active: str | None
    active: str | None
    switched_to: str | None
    fallback_strategy: str
    outcome: str
    recommended_account: str | None = None


def _parse_model_label(value: str) -> dict | None:
    label = value.strip()
    if not label:
        return None
    variant = None
    base = label
    match = re.match(r"^(?P<base>.+?) \((?P<variant>[^()]+)\)$", label)
    if match:
        base = match.group("base").strip()
        variant = match.group("variant").strip()
    provider = None
    family = None
    parts = base.split(None, 1)
    if parts:
        provider = parts[0].strip() or None
    if len(parts) > 1:
        family = parts[1].strip() or None
    return {
        "name": label,
        "provider": provider,
        "family": family,
        "variant": variant,
    }


def default_root() -> Path:
    return Path.home() / ".agy-cli-manager"


def default_live_dir() -> Path:
    env_live_dir = os.getenv("AGY_MANAGER_LIVE_DIR", "").strip()
    if env_live_dir:
        return Path(env_live_dir).expanduser()
    return Path.home() / ".gemini"


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
        save_state(
            paths,
            {
                "active": None,
                "accounts": {},
                "live_dir": str(default_live_dir()),
                "credential_backend": DEFAULT_CREDENTIAL_BACKEND,
                "switch_mode": DEFAULT_SWITCH_MODE,
                "switch_policy": _default_switch_policy(),
                "switch_runtime": _default_switch_runtime(),
                "switch_history": [],
            },
        )


@contextmanager
def manager_lock(paths: ManagerPaths):
    ensure_layout(paths)
    with paths.lock_file.open("a+", encoding="utf-8") as f:
        if os.name == "nt":
            # msvcrt.locking() requires an existing byte at the current
            # position and locks a byte range rather than the whole file.
            f.seek(0)
            f.write("0")
            f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
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
                if os.name == "nt":
                    # Keep the locked byte present until msvcrt releases it.
                    f.write("0")
                    f.truncate(1)
                    f.flush()
                else:
                    f.truncate()
            finally:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_state(paths: ManagerPaths) -> dict:
    ensure_layout(paths)
    with paths.state_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("active", None)
    data.setdefault("accounts", {})
    data.setdefault("live_dir", str(default_live_dir()))
    data["credential_backend"] = _normalize_credential_backend(data.get("credential_backend"))
    data["switch_mode"] = _normalize_switch_mode(data.get("switch_mode"))
    data["switch_policy"] = _normalize_switch_policy(data.get("switch_policy"))
    data["switch_runtime"] = _normalize_switch_runtime(data.get("switch_runtime"))
    data["switch_history"] = _normalize_switch_history(data.get("switch_history"))
    return data


def save_state(paths: ManagerPaths, state: dict) -> None:
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=paths.state_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, paths.state_file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _normalize_switch_mode(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_SWITCH_MODES:
            return normalized
    return DEFAULT_SWITCH_MODE


def _normalize_credential_backend(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_CREDENTIAL_BACKENDS:
            return normalized
    return DEFAULT_CREDENTIAL_BACKEND


def get_switch_mode(state: dict) -> str:
    return _normalize_switch_mode(state.get("switch_mode"))


def _default_switch_policy() -> dict:
    return {
        "short_usage_threshold_percent": DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT,
        "family_thresholds": {
            "gemini": DEFAULT_GEMINI_SWITCH_THRESHOLD_PERCENT,
            "other": DEFAULT_OTHER_SWITCH_THRESHOLD_PERCENT,
        },
        "refresh_failure_threshold": DEFAULT_REFRESH_FAILURE_SWITCH_THRESHOLD,
        "candidate_strategy": DEFAULT_CANDIDATE_STRATEGY,
        "family_fallback_strategy": DEFAULT_FAMILY_FALLBACK_STRATEGY,
    }


def _normalize_candidate_strategy(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_CANDIDATE_STRATEGIES:
            return normalized
    return DEFAULT_CANDIDATE_STRATEGY


def _normalize_family_fallback_strategy(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_FAMILY_FALLBACK_STRATEGIES:
            return normalized
    return DEFAULT_FAMILY_FALLBACK_STRATEGY


def _normalize_switch_policy(raw: object) -> dict:
    defaults = _default_switch_policy()
    policy = dict(defaults)
    if isinstance(raw, dict):
        threshold = raw.get("short_usage_threshold_percent")
        try:
            if threshold is not None:
                threshold_value = float(threshold)
                if 0.0 <= threshold_value <= 100.0:
                    policy["short_usage_threshold_percent"] = threshold_value
        except (TypeError, ValueError):
            pass
        shared_threshold = policy["short_usage_threshold_percent"]
        family_thresholds = raw.get("family_thresholds")
        normalized_family_thresholds = {
            "gemini": shared_threshold,
            "other": shared_threshold,
        }
        if isinstance(family_thresholds, dict):
            for family in USAGE_FAMILY_NAMES:
                try:
                    family_value = float(family_thresholds.get(family))
                    if 0.0 <= family_value <= 100.0:
                        normalized_family_thresholds[family] = family_value
                except (TypeError, ValueError):
                    pass
        policy["family_thresholds"] = normalized_family_thresholds
        failure_threshold = raw.get("refresh_failure_threshold")
        try:
            if failure_threshold is not None:
                failure_value = int(failure_threshold)
                if failure_value >= 1:
                    policy["refresh_failure_threshold"] = failure_value
        except (TypeError, ValueError):
            pass
        policy["candidate_strategy"] = _normalize_candidate_strategy(raw.get("candidate_strategy"))
        policy["family_fallback_strategy"] = _normalize_family_fallback_strategy(raw.get("family_fallback_strategy"))
    return policy


def _state_switch_policy(state: dict) -> dict:
    return _normalize_switch_policy(state.get("switch_policy"))


def _normalize_usage_family(value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if isinstance(value, str):
        family = value.strip().lower()
        if family in USAGE_FAMILY_NAMES:
            return family
    raise ValueError(f"Usage family must be one of: {', '.join(USAGE_FAMILY_NAMES)}")


def _default_proxy_config() -> dict:
    return {
        "enabled": False,
        "url": None,
        "label": None,
    }


def _normalize_proxy_config(raw: object) -> dict:
    proxy = _default_proxy_config()
    if isinstance(raw, dict):
        proxy["enabled"] = bool(raw.get("enabled", False))
        url = raw.get("url")
        label = raw.get("label")
        proxy["url"] = str(url).strip() or None if isinstance(url, str) else None
        proxy["label"] = str(label).strip() or None if isinstance(label, str) else None
    if not proxy["url"]:
        proxy["enabled"] = False
    return proxy


def _default_switch_runtime() -> dict:
    return {
        "status": "idle",
        "reason": None,
        "trigger": None,
        "request_id": None,
        "required_family": None,
        "active": None,
        "previous_active": None,
        "last_started_at": None,
        "last_completed_at": None,
    }


def _normalize_switch_runtime(raw: object) -> dict:
    runtime = _default_switch_runtime()
    if isinstance(raw, dict):
        for key in runtime:
            runtime[key] = raw.get(key)
    status = str(runtime.get("status") or "idle").strip().lower()
    if status not in {"idle", "switching", "ready", "no_account"}:
        status = "idle"
    runtime["status"] = status
    for key in ("reason", "trigger", "request_id", "active", "previous_active", "last_started_at", "last_completed_at"):
        value = runtime.get(key)
        runtime[key] = value if isinstance(value, str) or value is None else str(value)
    return runtime


def _mark_switch_runtime(
    state: dict,
    *,
    status: str,
    reason: str | None = None,
    trigger: str | None = None,
    request_id: str | None = None,
    required_family: str | None = None,
    active: str | None = None,
    previous_active: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> None:
    runtime = _normalize_switch_runtime(state.get("switch_runtime"))
    runtime["status"] = status
    runtime["reason"] = reason
    runtime["trigger"] = trigger
    runtime["request_id"] = request_id
    runtime["required_family"] = required_family
    runtime["active"] = active
    runtime["previous_active"] = previous_active
    if started_at is not None:
        runtime["last_started_at"] = started_at
    if completed_at is not None:
        runtime["last_completed_at"] = completed_at
    state["switch_runtime"] = runtime


def _normalize_switch_history(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    entries: list[dict] = []
    for item in raw[-DEFAULT_SWITCH_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        entries.append(
            {
                "at": item.get("at") if isinstance(item.get("at"), str) or item.get("at") is None else str(item.get("at")),
                "reason": item.get("reason") if isinstance(item.get("reason"), str) or item.get("reason") is None else str(item.get("reason")),
                "trigger": item.get("trigger") if isinstance(item.get("trigger"), str) or item.get("trigger") is None else str(item.get("trigger")),
                "request_id": item.get("request_id") if isinstance(item.get("request_id"), str) or item.get("request_id") is None else str(item.get("request_id")),
                "required_family": item.get("required_family") if item.get("required_family") in USAGE_FAMILY_NAMES else None,
                "previous_active": item.get("previous_active") if isinstance(item.get("previous_active"), str) or item.get("previous_active") is None else str(item.get("previous_active")),
                "active": item.get("active") if isinstance(item.get("active"), str) or item.get("active") is None else str(item.get("active")),
                "switched_to": item.get("switched_to") if isinstance(item.get("switched_to"), str) or item.get("switched_to") is None else str(item.get("switched_to")),
                "outcome": item.get("outcome") if isinstance(item.get("outcome"), str) or item.get("outcome") is None else str(item.get("outcome")),
                "cooldown_minutes": int(item.get("cooldown_minutes", 0) or 0),
            }
        )
    return entries


def _append_switch_history(
    state: dict,
    *,
    reason: str | None,
    trigger: str | None,
    request_id: str | None,
    previous_active: str | None,
    active: str | None,
    switched_to: str | None,
    outcome: str | None,
    cooldown_minutes: int,
    required_family: str | None = None,
    at: str | None = None,
) -> None:
    history = _normalize_switch_history(state.get("switch_history"))
    history.append(
        {
            "at": at or utc_now().isoformat(),
            "reason": reason,
            "trigger": trigger,
            "request_id": request_id,
            "required_family": required_family,
            "previous_active": previous_active,
            "active": active,
            "switched_to": switched_to,
            "outcome": outcome,
            "cooldown_minutes": int(cooldown_minutes or 0),
        }
    )
    state["switch_history"] = history[-DEFAULT_SWITCH_HISTORY_LIMIT:]


def account_dir(paths: ManagerPaths, name: str) -> Path:
    if not isinstance(name, str) or not name.strip() or name in {".", ".."}:
        raise ValueError("Account name must be a non-empty directory name.")
    if "/" in name or "\\" in name or Path(name).name != name:
        raise ValueError("Account name cannot contain path separators.")
    target = paths.accounts_dir / name
    if target.is_symlink():
        raise ValueError(f"Account directory cannot be a symlink: {name}")
    return target


def _clear_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def resolve_agy_binary(agy_binary: str | None = None) -> str:
    if agy_binary and agy_binary.strip():
        return agy_binary.strip()

    env_binary = os.getenv("AGY_BINARY", "").strip()
    if env_binary:
        return env_binary

    path_binary = shutil.which("agy")
    if path_binary:
        return path_binary

    install_sibling = Path(__file__).resolve().parents[3] / "agy"
    if install_sibling.is_file() and os.access(install_sibling, os.X_OK):
        return str(install_sibling)

    raise ValueError(
        "agy binary not found. Use --agy-binary, set AGY_BINARY, or put `agy` in PATH."
    )


def _copy_managed_profile_files(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in MANAGED_PROFILE_FILES:
        src = source / name
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, dst)
        else:
            dst.unlink(missing_ok=True)


def _remove_managed_profile_files(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in MANAGED_PROFILE_FILES:
        (target / name).unlink(missing_ok=True)


def _copy_account_profile(source_dir: Path, target_home: Path) -> None:
    profile_source = _resolve_profile_source(source_dir)
    target_profile = target_home / ".gemini"
    _copy_managed_profile_files(profile_source, target_profile)


def _write_private_credential(path: Path, blob: bytes, *, validate: bool = True) -> None:
    payload = bytes(blob)
    if validate:
        from agy_cli_manager.credential_store import validate_antigravity_credential

        payload = validate_antigravity_credential(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _configured_credential_backend(state: dict) -> str:
    return _normalize_credential_backend(state.get("credential_backend"))


def _effective_credential_backend(state: dict) -> str:
    configured = _configured_credential_backend(state)
    if configured == "file":
        return "file"
    from agy_cli_manager.credential_store import (
        linux_secret_service_available,
        read_linux_live_credential,
    )

    available = linux_secret_service_available()
    if configured == "secret-service" and not available:
        from agy_cli_manager.credential_store import CredentialStoreError

        raise CredentialStoreError(
            "The secret-service credential backend is configured but unavailable. "
            "Ensure a D-Bus session exists and install `secret-tool`."
        )
    if configured == "secret-service":
        return "secret-service"
    if not available:
        return "file"
    try:
        return "secret-service" if read_linux_live_credential(required=False) is not None else "file"
    except ValueError:
        return "file"


def _capture_linux_live_credential(profile_dir: Path, state: dict) -> bool:
    if _effective_credential_backend(state) != "secret-service":
        return False
    from agy_cli_manager.credential_store import read_linux_live_credential

    blob = read_linux_live_credential(required=True)
    _write_private_credential(profile_dir / MANAGED_PROFILE_FILES[0], blob)
    return True


def _snapshot_file(path: Path) -> tuple[bool, bytes | None]:
    return (path.is_file(), path.read_bytes() if path.is_file() else None)


def _restore_file(path: Path, snapshot: tuple[bool, bytes | None]) -> None:
    existed, content = snapshot
    if existed and content is not None:
        _write_private_credential(path, content, validate=False)
    else:
        path.unlink(missing_ok=True)


def _agy_processes_running() -> list[str]:
    if not sys.platform.startswith("linux"):
        return []
    running: list[str] = []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if name.lower() in {"agy", "agy.exe"}:
            running.append(f"{name} pid={entry.name}")
    return running


@contextmanager
def _isolated_linux_login_credential(state: dict, login_profile: Path):
    if _effective_credential_backend(state) != "secret-service":
        yield
        return
    if _agy_processes_running():
        raise ValueError("Refusing to start an isolated login while another agy process is running.")
    from agy_cli_manager.credential_store import (
        clear_linux_live_credential,
        read_linux_live_credential,
        restore_linux_live_credential,
    )

    original = read_linux_live_credential(required=False)
    if original is not None:
        clear_linux_live_credential()
    try:
        yield
        captured = read_linux_live_credential(required=True)
        _write_private_credential(login_profile / MANAGED_PROFILE_FILES[0], captured)
    finally:
        restore_linux_live_credential(original)


def _resolve_profile_source(source_dir: Path) -> Path:
    source_dir = source_dir.resolve()
    gemini_dir = source_dir / ".gemini"
    if gemini_dir.is_dir():
        return gemini_dir
    return source_dir


def _resolve_home_source(source_dir: Path) -> Path:
    source_dir = source_dir.resolve()
    if (source_dir / ".gemini").is_dir():
        return source_dir
    if source_dir.name == ".gemini":
        return source_dir.parent
    return source_dir


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _default_usage_window() -> dict:
    return {
        "status": "unknown",
        "value": None,
        "reset_at": None,
    }


def _default_usage_windows() -> dict:
    return {name: _default_usage_window() for name in USAGE_WINDOW_NAMES}


def _normalize_window_map(raw_windows: object) -> dict:
    windows = _default_usage_windows()
    if not isinstance(raw_windows, dict):
        return windows
    for name in USAGE_WINDOW_NAMES:
        raw = raw_windows.get(name)
        if not isinstance(raw, dict):
            continue
        windows[name] = {
            "status": raw.get("status", "unknown") or "unknown",
            "value": raw.get("value"),
            "reset_at": raw.get("reset_at"),
        }
    return windows


def _normalize_usage_windows(meta: dict) -> dict:
    raw_windows = meta.get("usage_windows")
    windows = _normalize_window_map(raw_windows)

    short_window = windows["short"]
    if short_window.get("value") is None and meta.get("usage_value") is not None:
        short_window["value"] = meta.get("usage_value")
    if short_window.get("status") == "unknown" and meta.get("usage_status") is not None:
        short_window["status"] = meta.get("usage_status") or "unknown"
    if short_window.get("reset_at") is None and meta.get("reset_at") is not None:
        short_window["reset_at"] = meta.get("reset_at")
    return windows


def _default_usage_families() -> dict:
    return {name: _default_usage_windows() for name in USAGE_FAMILY_NAMES}


def _normalize_usage_families(meta: dict) -> dict:
    raw_families = meta.get("usage_families")
    families = _default_usage_families()
    if isinstance(raw_families, dict):
        for name in USAGE_FAMILY_NAMES:
            families[name] = _normalize_window_map(raw_families.get(name))
    if not isinstance(raw_families, dict) or not isinstance(raw_families.get("gemini"), dict):
        families["gemini"] = _normalize_usage_windows(meta)
    return families


def _sync_legacy_usage_fields(meta: dict) -> None:
    families = _normalize_usage_families(meta)
    meta["usage_families"] = families
    windows = families["gemini"]
    meta["usage_windows"] = windows
    short_window = windows["short"]
    meta["usage_status"] = short_window.get("status", "unknown")
    meta["usage_value"] = short_window.get("value")
    meta["reset_at"] = short_window.get("reset_at")


def _normalize_timestamp(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    parsed = parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp value: {value}")
    return parsed.astimezone(timezone.utc).isoformat()


def get_live_dir(state: dict) -> Path | None:
    value = state.get("live_dir")
    if not value:
        return None
    return Path(value)


def resolve_runtime_home(live_dir: Path | None = None) -> Path:
    target_live_dir = live_dir or default_live_dir()
    return target_live_dir.parent


def _read_json_if_exists(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_text_if_exists(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _oauth_token_path(home_root: Path) -> Path:
    return home_root / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def _project_id_path(home_root: Path) -> Path:
    return home_root / ".gemini" / "antigravity-cli" / "cache" / "default_project_id.txt"


def _load_antigravity_token_state(home_root: Path) -> dict:
    path = _oauth_token_path(home_root)
    data = _read_json_if_exists(path)
    if not isinstance(data, dict):
        raise ValueError(f"Antigravity token file not found or invalid: {path}")
    token = data.get("token")
    if not isinstance(token, dict):
        raise ValueError(f"Antigravity token payload missing token object: {path}")
    return data


def _extract_access_token(home_root: Path) -> str:
    data = _load_antigravity_token_state(home_root)
    token = data.get("token")
    access_token = token.get("access_token") if isinstance(token, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Antigravity access token is missing.")
    return access_token.strip()


def _has_refresh_token(home_root: Path) -> bool:
    data = _load_antigravity_token_state(home_root)
    token = data.get("token")
    refresh_token = token.get("refresh_token") if isinstance(token, dict) else None
    return isinstance(refresh_token, str) and bool(refresh_token.strip())


def _token_expiry_due(home_root: Path, skew_seconds: int = 120) -> bool:
    data = _load_antigravity_token_state(home_root)
    token = data.get("token")
    expiry_raw = token.get("expiry") if isinstance(token, dict) else None
    if not isinstance(expiry_raw, str) or not expiry_raw.strip():
        return False
    expiry = parse_timestamp(expiry_raw.strip().replace("Z", "+00:00"))
    if expiry is None:
        return False
    return expiry <= utc_now() + timedelta(seconds=skew_seconds)


def _persist_project_id(home_root: Path, project_id: str | None) -> None:
    if not project_id:
        return
    path = _project_id_path(home_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(project_id.strip() + "\n", encoding="utf-8")


def _extract_project_id(load_response: dict, home_root: Path) -> str | None:
    project = load_response.get("cloudaicompanionProject")
    if isinstance(project, str) and project.strip():
        _persist_project_id(home_root, project.strip())
        return project.strip()
    if isinstance(project, dict):
        project_id = project.get("id")
        if isinstance(project_id, str) and project_id.strip():
            _persist_project_id(home_root, project_id.strip())
            return project_id.strip()
    cached = _read_text_if_exists(_project_id_path(home_root))
    return cached.strip() if isinstance(cached, str) and cached.strip() else None


def _cloudcode_request(access_token: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CODE_ASSIST_BASE_URL + path,
        data=body,
        headers={
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
            "User-Agent": CODE_ASSIST_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"Unexpected Cloud Code response type for {path}")
            return data
    except urllib.error.HTTPError as exc:
        message = ""
        try:
            payload_text = exc.read().decode("utf-8", "replace")
            payload_data = json.loads(payload_text)
            if isinstance(payload_data, dict):
                error_data = payload_data.get("error")
                if isinstance(error_data, dict) and isinstance(error_data.get("message"), str):
                    message = error_data["message"]
        except Exception:
            message = ""
        if exc.code == 401:
            raise PermissionError(message or "Cloud Code authentication failed.") from exc
        raise ValueError(message or f"Cloud Code request failed with HTTP {exc.code}.") from exc


def _google_userinfo_request(access_token: str) -> dict:
    req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={
            "Authorization": "Bearer " + access_token,
            "User-Agent": CODE_ASSIST_USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Unexpected Google userinfo response type.")
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("Google userinfo authentication failed.") from exc
        raise ValueError(f"Google userinfo request failed with HTTP {exc.code}.") from exc


def _run_agy_warmup(home_root: Path, agy_binary: str | None, timeout_seconds: int) -> None:
    resolved_binary = resolve_agy_binary(agy_binary)
    env = os.environ.copy()
    env["HOME"] = str(home_root)
    env["PATH"] = env.get("PATH", "/bin:/usr/bin:/usr/local/bin")
    if sys.platform.startswith("linux"):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/tmp/agy-cli-manager-isolated-no-keyring"
    proc = subprocess.run(
        [
            resolved_binary,
            "--dangerously-skip-permissions",
            "-p",
            "reply with one word: pong",
        ],
        cwd=home_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=max(10, timeout_seconds),
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise ValueError(f"agy warmup failed: {detail[:200]}")


def _parse_summary_bucket(bucket: dict) -> dict:
    remaining = bucket.get("remainingFraction")
    reset_raw = bucket.get("resetTime")
    reset_at = None
    if isinstance(reset_raw, str):
        reset_at = _normalize_timestamp(reset_raw.replace("Z", "+00:00"))
    return {
        "status": "known" if isinstance(remaining, (int, float)) or reset_at else "unknown",
        "value": round(float(remaining) * 100, 2) if isinstance(remaining, (int, float)) else None,
        "reset_at": reset_at,
    }


def _select_quota_summary_group(summary_response: dict) -> dict | None:
    groups = summary_response.get("groups")
    if not isinstance(groups, list):
        return None
    normalized = [group for group in groups if isinstance(group, dict)]
    if not normalized:
        return None
    for group in normalized:
        display_name = group.get("displayName")
        if isinstance(display_name, str) and "gemini" in display_name.lower():
            return group
    return normalized[0]


def _quota_group_family(group: dict) -> str | None:
    buckets = group.get("buckets")
    if isinstance(buckets, list):
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_id = str(bucket.get("bucketId") or "").lower()
            if bucket_id.startswith("gemini-"):
                return "gemini"
            if bucket_id.startswith("3p-"):
                return "other"
    label = " ".join(
        str(group.get(key) or "") for key in ("displayName", "description")
    ).lower()
    if "gemini" in label:
        return "gemini"
    if "claude" in label or "gpt" in label:
        return "other"
    return None


def _parse_quota_families_from_summary(summary_response: dict) -> tuple[dict, int]:
    families = _default_usage_families()
    groups = summary_response.get("groups")
    if not isinstance(groups, list):
        return families, 0
    bucket_count = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        family = _quota_group_family(group)
        buckets = group.get("buckets")
        if family not in USAGE_FAMILY_NAMES or not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_count += 1
            window_name = bucket.get("window")
            if window_name == "5h":
                families[family]["short"] = _parse_summary_bucket(bucket)
            elif window_name == "weekly":
                families[family]["weekly"] = _parse_summary_bucket(bucket)
    return families, bucket_count


def _parse_quota_windows_from_summary(summary_response: dict) -> tuple[dict, dict, int]:
    families, bucket_count = _parse_quota_families_from_summary(summary_response)
    return families["gemini"]["short"], families["gemini"]["weekly"], bucket_count


def _resolve_usage_refresh_target(paths: ManagerPaths, state: dict, name: str | None) -> tuple[str, Path]:
    account_name = name or state.get("active")
    if not account_name:
        raise ValueError("No active account is set.")
    if account_name not in state["accounts"]:
        raise ValueError(f"Account not found: {account_name}")
    if name is None:
        live_dir = get_live_dir(state)
        if live_dir is not None:
            return account_name, live_dir.parent
        return account_name, paths.runtime_dir
    return account_name, account_dir(paths, account_name)


def _run_agy_models_command(
    runtime_home: Path,
    agy_binary: str | None = None,
    timeout_seconds: int = 30,
) -> list[dict]:
    resolved_binary = resolve_agy_binary(agy_binary)
    env = os.environ.copy()
    env["HOME"] = str(runtime_home)
    env["PATH"] = env.get("PATH", "/bin:/usr/bin:/usr/local/bin")
    if sys.platform.startswith("linux"):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/tmp/agy-cli-manager-isolated-no-keyring"
    proc = subprocess.run(
        [resolved_binary, "models"],
        cwd=runtime_home,
        env=env,
        capture_output=True,
        text=True,
        timeout=max(10, timeout_seconds),
        check=False,
    )
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    if proc.returncode != 0:
        tail = "\n".join(output.splitlines()[-8:]) if output else "no output"
        raise ValueError(f"agy models failed with exit code {proc.returncode}: {tail}")
    models: list[dict] = []
    for line in output.splitlines():
        parsed = _parse_model_label(line)
        if parsed:
            models.append(parsed)
    if not models:
        raise ValueError("agy models returned no usable model entries.")
    return models


def _account_due_for_refresh(meta: dict, now: datetime | None = None) -> bool:
    current = now or utc_now()
    if not isinstance(meta, dict):
        return False
    if not meta.get("enabled", True):
        return False
    status = meta.get("status") or "standby"
    if status in {"disabled", "cooldown"}:
        return False
    next_check = parse_timestamp(meta.get("next_live_check_at"))
    if next_check is not None:
        return next_check <= current
    policy = int(meta.get("refresh_policy_seconds", DEFAULT_REFRESH_POLICY_SECONDS) or DEFAULT_REFRESH_POLICY_SECONDS)
    if policy <= 0:
        return False
    last_check = parse_timestamp(meta.get("last_live_check_at"))
    if last_check is None:
        return True
    return last_check + timedelta(seconds=policy) <= current


def _eligible_switch_candidates(state: dict, exclude: str | None = None) -> list[str]:
    return [
        name
        for name, meta in sorted(state["accounts"].items())
        if name != exclude
        and meta.get("enabled", True)
        and meta.get("status") != "cooldown"
    ]


def _usage_windows_for_family(meta: dict, family: str) -> dict:
    normalized_family = _normalize_usage_family(family)
    families = _normalize_usage_families(meta)
    return families[normalized_family]


def _is_short_window_exhausted(
    meta: dict,
    now: datetime | None = None,
    *,
    threshold_percent: float = DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT,
    family: str = "gemini",
) -> bool:
    return _is_usage_window_exhausted(
        meta,
        "short",
        now,
        threshold_percent=threshold_percent,
        family=family,
    )


def _is_usage_window_exhausted(
    meta: dict,
    window_name: str,
    now: datetime | None = None,
    *,
    threshold_percent: float = DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT,
    family: str = "gemini",
) -> bool:
    current = now or utc_now()
    windows = _usage_windows_for_family(meta, family)
    window = windows.get(window_name, {})
    if window.get("status") != "known":
        return False
    value = _coerce_usage_value(window.get("value"))
    if value is None or value > threshold_percent:
        return False
    reset_at = parse_timestamp(window.get("reset_at"))
    if reset_at is not None and reset_at <= current:
        return False
    return True


def _is_family_quota_exhausted(
    meta: dict,
    now: datetime | None = None,
    *,
    threshold_percent: float = DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT,
    family: str = "gemini",
) -> bool:
    return any(
        _is_usage_window_exhausted(
            meta,
            window_name,
            now,
            threshold_percent=threshold_percent,
            family=family,
        )
        for window_name in USAGE_WINDOW_NAMES
    )


def _cooldown_minutes_from_family_quota(
    meta: dict,
    now: datetime | None = None,
    *,
    threshold_percent: float = DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT,
    family: str = "gemini",
) -> int:
    current = now or utc_now()
    windows = _usage_windows_for_family(meta, family)
    blocking_resets = [
        reset_at
        for window_name in USAGE_WINDOW_NAMES
        if _is_usage_window_exhausted(
            meta,
            window_name,
            current,
            threshold_percent=threshold_percent,
            family=family,
        )
        for reset_at in [parse_timestamp(windows.get(window_name, {}).get("reset_at"))]
        if reset_at is not None and reset_at > current
    ]
    if not blocking_resets:
        return 60
    delta_seconds = max(60.0, (max(blocking_resets) - current).total_seconds())
    return max(1, int(math.ceil(delta_seconds / 60.0)))


def _refresh_failure_threshold_reached(meta: dict, threshold: int = DEFAULT_REFRESH_FAILURE_SWITCH_THRESHOLD) -> bool:
    return int(meta.get("refresh_fail_count", 0) or 0) >= threshold


def _coerce_usage_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _candidate_usage_value(meta: dict, window_name: str, *, family: str = "gemini") -> float | None:
    windows = _usage_windows_for_family(meta, family)
    window = windows.get(window_name, {})
    if not isinstance(window, dict):
        return None
    return _coerce_usage_value(window.get("value"))


def _candidate_health_priority(health: str) -> int:
    order = {
        "healthy": 0,
        "ready": 1,
        "stale": 2,
        "refresh_failed": 3,
        "auth_expired": 4,
        "auth_missing": 5,
        "cooldown": 6,
        "disabled": 7,
    }
    return order.get(health, 8)


def _family_cooldown_active(meta: dict, family: str, now: datetime | None = None) -> bool:
    cooldowns = meta.get("family_cooldowns")
    if not isinstance(cooldowns, dict):
        return False
    until = parse_timestamp(cooldowns.get(family))
    return until is not None and until > (now or utc_now())


def _best_switch_candidate(
    paths: ManagerPaths,
    state: dict,
    *,
    exclude: str | None = None,
    required_family: str | None = None,
) -> str | None:
    policy = _state_switch_policy(state)
    strategy = policy["candidate_strategy"]
    family = _normalize_usage_family(required_family, allow_none=True)
    threshold_percent = float(
        policy["family_thresholds"].get(family, policy["short_usage_threshold_percent"])
    )
    candidates = _eligible_switch_candidates(state, exclude=exclude)
    if not candidates:
        return None

    ranked: list[tuple[tuple[object, ...], str]] = []
    current = utc_now()
    for name in candidates:
        meta = state["accounts"].get(name)
        if not isinstance(meta, dict):
            continue
        health = _derive_health_status(paths, name, meta)
        if health in {"auth_missing", "auth_expired", "disabled", "cooldown"}:
            continue
        if family is not None and _family_cooldown_active(meta, family, current):
            continue

        short_value = _candidate_usage_value(meta, "short", family=family or "gemini")
        weekly_value = _candidate_usage_value(meta, "weekly", family=family or "gemini")
        short_known = short_value is not None
        quota_low = _is_family_quota_exhausted(
            meta,
            current,
            threshold_percent=threshold_percent,
            family=family or "gemini",
        )
        weekly_known = weekly_value is not None

        # When a caller names the family it needs, never fail over onto an
        # account already known to be exhausted for that same family. Unknown
        # quota remains eligible, but ranks behind known usable quota.
        if family is not None and quota_low:
            continue

        if strategy == "highest-short":
            score = (
                0 if short_known else 1,
                -(short_value if short_value is not None else -1.0),
                _candidate_health_priority(health),
                int(meta.get("refresh_fail_count", 0) or 0),
                int(meta.get("fail_count", 0) or 0),
                str(meta.get("created_at") or ""),
                name.lower(),
            )
        elif strategy == "round-robin":
            score = (
                _candidate_health_priority(health),
                0 if short_known and not quota_low else 1,
                str(meta.get("created_at") or ""),
                name.lower(),
            )
        else:
            score = (
                _candidate_health_priority(health),
                0 if short_known and not quota_low else 1,
                0 if short_known else 1,
                -(short_value if short_value is not None else -1.0),
                0 if weekly_known else 1,
                -(weekly_value if weekly_value is not None else -1.0),
                int(meta.get("refresh_fail_count", 0) or 0),
                int(meta.get("fail_count", 0) or 0),
                str(meta.get("created_at") or ""),
                name.lower(),
            )
        ranked.append((score, name))

    if not ranked:
        return None

    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _account_can_serve_family(
    paths: ManagerPaths,
    name: str,
    meta: dict,
    family: str,
    policy: dict,
    now: datetime,
) -> bool:
    health = _derive_health_status(paths, name, meta)
    if health in {"auth_missing", "auth_expired", "disabled", "cooldown"}:
        return False
    if _family_cooldown_active(meta, family, now):
        return False
    threshold = float(policy["family_thresholds"][family])
    return not _is_family_quota_exhausted(meta, now, threshold_percent=threshold, family=family)


def resolve_route(
    paths: ManagerPaths,
    preferred_family: str,
    *,
    fallback_strategy: str | None = None,
    force_switch: bool = False,
) -> RouteResult:
    """Resolve and apply an account/family route for an external caller.

    The manager applies an account switch when policy permits it, but only
    returns the selected family. The caller remains responsible for selecting
    a concrete model from that family.
    """
    family = _normalize_usage_family(preferred_family)
    alternate = "other" if family == "gemini" else "gemini"
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        policy = _state_switch_policy(state)
        strategy = (
            _normalize_family_fallback_strategy(fallback_strategy)
            if fallback_strategy is not None
            else policy["family_fallback_strategy"]
        )
        if fallback_strategy is not None and strategy != fallback_strategy.strip().lower():
            raise ValueError(f"Unsupported family fallback strategy: {fallback_strategy}")

        previous = state.get("active")
        current_meta = state["accounts"].get(previous) if previous else None
        now = utc_now()
        if isinstance(current_meta, dict) and _account_can_serve_family(
            paths, previous, current_meta, family, policy, now
        ):
            return RouteResult(family, family, previous, previous, None, strategy, "active_ready")

        same_family_account = _best_switch_candidate(
            paths, state, exclude=previous, required_family=family
        )
        alternate_current = (
            previous
            if isinstance(current_meta, dict)
            and strategy != "strict-family"
            and _account_can_serve_family(paths, previous, current_meta, alternate, policy, now)
            else None
        )
        alternate_account = (
            _best_switch_candidate(paths, state, exclude=previous, required_family=alternate)
            if strategy != "strict-family"
            else None
        )

        if strategy == "same-account-first":
            choices = ((alternate_current, alternate), (same_family_account, family), (alternate_account, alternate))
        elif strategy == "strict-family":
            choices = ((same_family_account, family),)
        else:
            choices = ((same_family_account, family), (alternate_current, alternate), (alternate_account, alternate))

        selected_account = None
        selected_family = None
        for account_name, candidate_family in choices:
            if account_name:
                selected_account = account_name
                selected_family = candidate_family
                break
        if selected_account is None or selected_family is None:
            return RouteResult(family, None, previous, previous, None, strategy, "no_route")

        if selected_account == previous:
            return RouteResult(family, selected_family, previous, previous, None, strategy, "family_fallback")
        if get_switch_mode(state) != "auto" and not force_switch:
            return RouteResult(family, selected_family, previous, previous, None, strategy, "switch_required", selected_account)

        state = _activate_account_locked(paths, state, selected_account)
        save_state(paths, state)
        outcome = "account_switch" if selected_family == family else "account_and_family_fallback"
        return RouteResult(family, selected_family, previous, selected_account, selected_account, strategy, outcome)


def pick_due_refresh_account(paths: ManagerPaths) -> str | None:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        now = utc_now()
        active_name = state.get("active")
        if active_name:
            active_meta = state["accounts"].get(active_name)
            if isinstance(active_meta, dict) and _account_due_for_refresh(active_meta, now):
                return active_name
        for name, meta in sorted(state["accounts"].items()):
            if name == active_name:
                continue
            if _account_due_for_refresh(meta, now):
                return name
    return None


def ensure_active_account(
    paths: ManagerPaths,
    *,
    force: bool = False,
    required_family: str | None = None,
) -> EnsureActiveResult:
    family = _normalize_usage_family(required_family, allow_none=True)
    snapshot = get_status_snapshot(paths)
    switch_mode = snapshot.get("switch_mode", DEFAULT_SWITCH_MODE)
    switch_policy = snapshot.get("switch_policy") or _default_switch_policy()
    active_name = snapshot.get("active")
    accounts = snapshot.get("accounts", {})
    now = utc_now()

    if switch_mode != "auto" and not force:
        return EnsureActiveResult(
            triggered=False,
            switch_mode=switch_mode,
            previous_active=active_name,
            active=active_name,
            switched_to=None,
            reason=None,
            cooldown_minutes=0,
            required_family=family,
        )

    if not active_name:
        with manager_lock(paths):
            state = sync_state_from_disk(paths, load_state(paths))
            switched_to = _best_switch_candidate(paths, state, required_family=family)
            if switched_to:
                state = _activate_account_locked(paths, state, switched_to)
                save_state(paths, state)
        if not switched_to:
            return EnsureActiveResult(
                triggered=False,
                switch_mode=switch_mode,
                previous_active=None,
                active=None,
                switched_to=None,
                reason="no_active_account",
                cooldown_minutes=0,
                required_family=family,
            )
        return EnsureActiveResult(
            triggered=True,
            switch_mode=switch_mode,
            previous_active=None,
            active=switched_to,
            switched_to=switched_to,
            reason="no_active_account",
            cooldown_minutes=0,
            required_family=family,
        )

    active_meta = accounts.get(active_name)
    if not isinstance(active_meta, dict):
        with manager_lock(paths):
            state = sync_state_from_disk(paths, load_state(paths))
            switched_to = _best_switch_candidate(paths, state, exclude=active_name, required_family=family)
            if switched_to:
                state = _activate_account_locked(paths, state, switched_to)
                save_state(paths, state)
        if not switched_to:
            return EnsureActiveResult(
                triggered=False,
                switch_mode=switch_mode,
                previous_active=active_name,
                active=None,
                switched_to=None,
                reason="active_missing",
                cooldown_minutes=0,
                required_family=family,
            )
        return EnsureActiveResult(
            triggered=True,
            switch_mode=switch_mode,
            previous_active=active_name,
            active=switched_to,
            switched_to=switched_to,
            reason="active_missing",
            cooldown_minutes=0,
            required_family=family,
        )

    reason = None
    cooldown_minutes = 0
    health = active_meta.get("health_status")
    if health in {"auth_missing", "auth_expired"}:
        reason = health
        cooldown_minutes = 60
    elif _is_family_quota_exhausted(
        active_meta,
        now,
        threshold_percent=float(
            switch_policy["family_thresholds"].get(
                family or "gemini",
                switch_policy.get("short_usage_threshold_percent", DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT),
            )
        ),
        family=family or "gemini",
    ):
        reason = f"{family}_quota_exhausted" if family else "quota_exhausted"
        cooldown_minutes = _cooldown_minutes_from_family_quota(
            active_meta,
            now,
            threshold_percent=float(
                switch_policy["family_thresholds"].get(
                    family or "gemini",
                    switch_policy.get("short_usage_threshold_percent", DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT),
                )
            ),
            family=family or "gemini",
        )
    elif _refresh_failure_threshold_reached(
        active_meta,
        threshold=int(switch_policy.get("refresh_failure_threshold", DEFAULT_REFRESH_FAILURE_SWITCH_THRESHOLD)),
    ):
        reason = "refresh_failed"
        cooldown_minutes = 10

    if reason is None:
        return EnsureActiveResult(
            triggered=False,
            switch_mode=switch_mode,
            previous_active=active_name,
            active=active_name,
            switched_to=None,
            reason=None,
            cooldown_minutes=0,
            required_family=family,
        )

    result = rotate_after_failure(
        paths,
        reason=reason,
        cooldown_minutes=cooldown_minutes,
        force_switch=True,
        required_family=family,
    )
    return EnsureActiveResult(
        triggered=bool(result.switched_to or result.previous_active),
        switch_mode=switch_mode,
        previous_active=result.previous_active,
        active=result.active,
        switched_to=result.switched_to,
        reason=reason,
        cooldown_minutes=cooldown_minutes,
        required_family=family,
    )


def refresh_due_account(
    paths: ManagerPaths,
    *,
    agy_binary: str | None = None,
    warmup_timeout_seconds: int = 45,
) -> UsageRefreshResult | None:
    ensure_active_account(paths)
    target = pick_due_refresh_account(paths)
    if target is None:
        return None
    return refresh_account_usage(
        paths,
        target,
        agy_binary=agy_binary,
        warmup_timeout_seconds=warmup_timeout_seconds,
    )


def list_models(
    paths: ManagerPaths,
    name: str | None = None,
    *,
    agy_binary: str | None = None,
    timeout_seconds: int = 30,
) -> dict:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        account_name, source_home = _resolve_usage_refresh_target(paths, state, name)
    if not profile_has_login_artifacts(_resolve_profile_source(source_home)):
        fallback_home = account_dir(paths, account_name)
        if name is None and profile_has_login_artifacts(_resolve_profile_source(fallback_home)):
            source_home = fallback_home
        else:
            raise ValueError(f"Profile source is missing required auth files: {_resolve_profile_source(source_home)}")

    models = _run_agy_models_command(source_home, agy_binary=agy_binary, timeout_seconds=timeout_seconds)
    return {
        "account": account_name,
        "source_home": str(source_home),
        "models": models,
        "count": len(models),
    }


def _persist_refresh_failure(paths: ManagerPaths, account_name: str, error: str) -> None:
    failed_at = utc_now()
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(account_name)
        if meta is None:
            return
        meta["health_status"] = "refresh_failed"
        meta["last_live_check_error"] = error
        meta["refresh_fail_count"] = int(meta.get("refresh_fail_count", 0) or 0) + 1
        meta["next_live_check_at"] = _normalize_timestamp(failed_at + timedelta(minutes=5))
        save_state(paths, state)


def refresh_account_usage(
    paths: ManagerPaths,
    name: str | None = None,
    *,
    agy_binary: str | None = None,
    warmup_timeout_seconds: int = 45,
) -> UsageRefreshResult:
    live_home = None
    initial_live_token = None
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        account_name, _ = _resolve_usage_refresh_target(paths, state, name)
        source_home = account_dir(paths, account_name)
        if name is None and state.get("active") == account_name:
            live_dir = get_live_dir(state)
            if live_dir is not None:
                live_home = live_dir.parent
                live_token = _oauth_token_path(live_home)
                if live_token.is_file():
                    initial_live_token = live_token.read_bytes()
                    _copy_managed_profile_files(_resolve_profile_source(live_home), source_home / ".gemini")
    try:
        needs_warmup = False
        try:
            access_token = _extract_access_token(source_home)
            needs_warmup = _token_expiry_due(source_home)
        except ValueError:
            needs_warmup = True
            access_token = None

        if needs_warmup:
            _run_agy_warmup(source_home, agy_binary, warmup_timeout_seconds)
            access_token = _extract_access_token(source_home)

        try:
            load_response = _cloudcode_request(
                access_token,
                CODE_ASSIST_LOAD_PATH,
                {
                    "metadata": {
                        "ideType": "ANTIGRAVITY",
                        "platform": "PLATFORM_UNSPECIFIED",
                        "pluginType": "GEMINI",
                    }
                },
            )
        except PermissionError:
            _run_agy_warmup(source_home, agy_binary, warmup_timeout_seconds)
            access_token = _extract_access_token(source_home)
            load_response = _cloudcode_request(
                access_token,
                CODE_ASSIST_LOAD_PATH,
                {
                    "metadata": {
                        "ideType": "ANTIGRAVITY",
                        "platform": "PLATFORM_UNSPECIFIED",
                        "pluginType": "GEMINI",
                    }
                },
            )

        project_id = _extract_project_id(load_response, source_home)
        if not project_id:
            raise ValueError("Cloud Code project id is unavailable.")

        quota_response = _cloudcode_request(access_token, CODE_ASSIST_QUOTA_SUMMARY_PATH, {"project": project_id})
        usage_families, bucket_count = _parse_quota_families_from_summary(quota_response)
        short_window = usage_families["gemini"]["short"]
        weekly_window = usage_families["gemini"]["weekly"]
        plan_info = load_response.get("planInfo")
        plan_type = plan_info.get("planType") if isinstance(plan_info, dict) else None
        monthly = plan_info.get("monthlyPromptCredits") if isinstance(plan_info, dict) else None
        available = load_response.get("availablePromptCredits")

        result = UsageRefreshResult(
            account=account_name,
            source_home=str(source_home),
            project_id=project_id,
            plan_type=plan_type if isinstance(plan_type, str) else None,
            prompt_credits_available=available if isinstance(available, (int, float)) else None,
            prompt_credits_monthly=monthly if isinstance(monthly, (int, float)) else None,
            short_usage_status=short_window.get("status", "unknown"),
            short_usage_value=short_window.get("value"),
            short_reset_at=short_window.get("reset_at"),
            weekly_usage_status=weekly_window.get("status", "unknown"),
            weekly_usage_value=weekly_window.get("value"),
            weekly_reset_at=weekly_window.get("reset_at"),
            usage_families=usage_families,
            bucket_count=bucket_count,
        )

        refreshed_at = utc_now()
        refreshed_identity = detect_profile_identity(account_dir(paths, account_name))
        if not refreshed_identity.get("account_name") and isinstance(access_token, str) and access_token.strip():
            try:
                live_identity = _best_effort_live_identity(access_token.strip())
                if live_identity:
                    refreshed_identity = live_identity
            except (PermissionError, ValueError):
                pass
        with manager_lock(paths):
            state = sync_state_from_disk(paths, load_state(paths))
            meta = state["accounts"].get(account_name)
            if meta is None:
                raise ValueError(f"Account not found: {account_name}")
            # A warmup may refresh the saved token. Publish it only if the
            # same account is still active and agy has not updated live auth.
            if live_home is not None and initial_live_token is not None and state.get("active") == account_name:
                live_token = _oauth_token_path(live_home)
                saved_token = _oauth_token_path(source_home)
                if live_token.is_file() and saved_token.is_file() and live_token.read_bytes() == initial_live_token:
                    shutil.copy2(saved_token, live_token)
            meta["usage_families"] = result.usage_families
            meta["health_status"] = "healthy"
            meta["last_live_check_at"] = _normalize_timestamp(refreshed_at)
            meta["last_live_check_error"] = None
            meta["refresh_fail_count"] = 0
            policy_seconds = int(meta.get("refresh_policy_seconds", DEFAULT_REFRESH_POLICY_SECONDS) or DEFAULT_REFRESH_POLICY_SECONDS)
            meta["next_live_check_at"] = _normalize_timestamp(refreshed_at + timedelta(seconds=policy_seconds))
            meta["identity"] = refreshed_identity
            _sync_legacy_usage_fields(meta)
            save_state(paths, state)

        ensure_active_account(paths)
        return result
    except Exception as exc:
        _persist_refresh_failure(paths, account_name, str(exc))
        try:
            ensure_active_account(paths)
        except ValueError:
            pass
        raise


def _decode_jwt_payload(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        return json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _identity_from_payload(payload: dict, source: str) -> dict | None:
    if not isinstance(payload, dict):
        return None
    email = payload.get("email")
    name = payload.get("name")
    subject = payload.get("sub") or payload.get("id")
    account_name = None
    if isinstance(email, str) and email.strip():
        account_name = email.strip()
    elif isinstance(name, str) and name.strip():
        account_name = name.strip()
    elif isinstance(subject, str) and subject.strip():
        account_name = subject.strip()
    if not account_name:
        return None
    identity = {
        "account_name": account_name,
        "source": source,
    }
    if isinstance(email, str) and email.strip():
        identity["email"] = email.strip()
    if isinstance(name, str) and name.strip():
        identity["display_name"] = name.strip()
    if isinstance(subject, str) and subject.strip():
        identity["subject"] = subject.strip()
    return identity


def _identity_from_google_accounts(google_accounts: dict | list) -> dict | None:
    if isinstance(google_accounts, dict):
        active = google_accounts.get("active")
        if isinstance(active, str) and active.strip():
            identity = {
                "account_name": active.strip(),
                "source": "google_accounts.json.active",
            }
            if "@" in active:
                identity["email"] = active.strip()
            return identity
        accounts = google_accounts.get("accounts") or google_accounts.get("old")
        if isinstance(accounts, list):
            for entry in accounts:
                if isinstance(entry, str) and entry.strip() and "@" in entry:
                    return {
                        "account_name": entry.strip(),
                        "email": entry.strip(),
                        "source": "google_accounts.json.accounts",
                    }
                if isinstance(entry, dict):
                    identity = _identity_from_payload(entry, "google_accounts.json.accounts")
                    if identity:
                        return identity
    elif isinstance(google_accounts, list):
        for entry in google_accounts:
            if isinstance(entry, dict):
                identity = _identity_from_payload(entry, "google_accounts.json")
                if identity:
                    return identity
            elif isinstance(entry, str) and entry.strip() and "@" in entry:
                return {
                    "account_name": entry.strip(),
                    "email": entry.strip(),
                    "source": "google_accounts.json",
                }
    return None


def _identity_from_oauth_creds(oauth_creds: dict) -> dict | None:
    if not isinstance(oauth_creds, dict):
        return None
    direct_identity = _identity_from_payload(oauth_creds, "oauth_creds.json")
    if direct_identity:
        return direct_identity
    for key in ("user", "user_info", "userinfo", "profile"):
        nested = oauth_creds.get(key)
        if isinstance(nested, dict):
            nested_identity = _identity_from_payload(nested, f"oauth_creds.json.{key}")
            if nested_identity:
                return nested_identity
    for token_key in ("id_token", "token", "access_token"):
        token_value = oauth_creds.get(token_key)
        if isinstance(token_value, str) and token_value.strip() and token_value.count(".") >= 2:
            payload = _decode_jwt_payload(token_value.strip())
            identity = _identity_from_payload(payload or {}, f"oauth_creds.json.{token_key}")
            if identity:
                return identity
    return None


def _identity_from_antigravity_token(token_state: dict) -> dict | None:
    if not isinstance(token_state, dict):
        return None
    direct_identity = _identity_from_payload(token_state, "antigravity-oauth-token")
    if direct_identity:
        return direct_identity
    token = token_state.get("token")
    if isinstance(token, dict):
        token_identity = _identity_from_payload(token, "antigravity-oauth-token.token")
        if token_identity:
            return token_identity
        for token_key in ("id_token", "access_token"):
            token_value = token.get(token_key)
            if isinstance(token_value, str) and token_value.strip() and token_value.count(".") >= 2:
                payload = _decode_jwt_payload(token_value.strip())
                identity = _identity_from_payload(payload or {}, f"antigravity-oauth-token.token.{token_key}")
                if identity:
                    return identity
    return None


def _iter_antigravity_log_files(home_root: Path) -> list[Path]:
    base_dir = home_root / ".gemini" / "antigravity-cli"
    candidates: list[Path] = []
    cli_log = base_dir / "cli.log"
    if cli_log.is_file():
        candidates.append(cli_log)
    log_dir = base_dir / "log"
    if log_dir.is_dir():
        try:
            log_files = sorted(
                (path for path in log_dir.iterdir() if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            log_files = []
        candidates.extend(log_files)
    return candidates


def _identity_from_antigravity_logs(source_dir: Path) -> dict | None:
    home_root = _resolve_home_source(source_dir)
    for path in _iter_antigravity_log_files(home_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(text.splitlines()):
            match = APPLY_AUTH_EMAIL_PATTERN.search(line)
            if match:
                email = match.group(1).strip()
                if email:
                    return {
                        "account_name": email,
                        "email": email,
                        "source": f"antigravity-cli.log:{path.name}",
                    }
            if "Cache(userInfo)" in line:
                match = EMAIL_PATTERN.search(line)
                if match:
                    email = match.group(0).strip()
                    if email:
                        return {
                            "account_name": email,
                            "email": email,
                            "source": f"antigravity-cli.log:{path.name}",
                        }
    return None


def _best_effort_live_identity(access_token: str) -> dict | None:
    userinfo = _google_userinfo_request(access_token)
    return _identity_from_payload(userinfo, "google_userinfo")


def _best_effort_saved_profile_identity(source_dir: Path) -> dict:
    identity = detect_profile_identity(source_dir)
    if identity.get("account_name"):
        return identity
    log_identity = _identity_from_antigravity_logs(source_dir)
    if log_identity:
        return log_identity
    home_source = _resolve_home_source(source_dir)
    try:
        access_token = _extract_access_token(home_source)
    except ValueError:
        return identity
    if not isinstance(access_token, str) or not access_token.strip():
        return identity
    try:
        live_identity = _best_effort_live_identity(access_token.strip())
    except (PermissionError, ValueError, urllib.error.URLError):
        return identity
    return live_identity or identity


def detect_profile_identity(source_dir: Path) -> dict:
    profile_source = _resolve_profile_source(source_dir)
    google_accounts = _read_json_if_exists(profile_source / "google_accounts.json")
    if google_accounts is not None:
        identity = _identity_from_google_accounts(google_accounts)
        if identity:
            return identity

    google_account_id = _read_text_if_exists(profile_source / "google_account_id")
    if google_account_id:
        identity = {
            "account_name": google_account_id,
            "source": "google_account_id",
        }
        if "@" in google_account_id:
            identity["email"] = google_account_id
        return identity

    oauth_creds = _read_json_if_exists(profile_source / "oauth_creds.json")
    if isinstance(oauth_creds, dict):
        identity = _identity_from_oauth_creds(oauth_creds)
        if identity:
            return identity

    try:
        token_state = _load_antigravity_token_state(_resolve_home_source(source_dir))
    except ValueError:
        token_state = None
    if isinstance(token_state, dict):
        identity = _identity_from_antigravity_token(token_state)
        if identity:
            return identity
    identity = _identity_from_antigravity_logs(source_dir)
    if identity:
        return identity

    return {
        "account_name": None,
        "source": "unavailable",
    }


def normalize_account_storage_name(value: str) -> str:
    cleaned = value.strip().replace("/", "_").replace("\\", "_")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise ValueError("Detected account name is empty.")
    if cleaned in {".", ".."}:
        raise ValueError("Detected account name is not usable as a storage path.")
    return cleaned


def next_available_account_name(paths: ManagerPaths, base_name: str) -> str:
    candidate = base_name
    suffix = 2
    while account_dir(paths, candidate).exists():
        candidate = f"{base_name}.{suffix}"
        suffix += 1
    return candidate


def _update_account_identity(state: dict, name: str, identity: dict) -> None:
    meta = state["accounts"].setdefault(name, {})
    meta["identity"] = identity


def refresh_account_identity(paths: ManagerPaths, name: str) -> dict:
    identity = _best_effort_saved_profile_identity(account_dir(paths, name))
    if not identity.get("account_name"):
        try:
            live_dir = get_live_dir(load_state(paths))
            probe = probe_profile_identity_via_usage(
                account_dir(paths, name),
                live_dir=live_dir,
            )
            if probe.get("account_name"):
                identity = probe
        except (ValueError, subprocess.TimeoutExpired):
            pass
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        if name not in state["accounts"]:
            raise ValueError(f"Account not found: {name}")
        _update_account_identity(state, name, identity)
        save_state(paths, state)
    return identity


def get_account_identity(paths: ManagerPaths, name: str | None = None) -> tuple[str, dict]:
    state = sync_state_from_disk(paths, load_state(paths))
    resolved_name = name or state.get("active")
    if not resolved_name:
        raise ValueError("No active account is set.")
    if resolved_name not in state["accounts"]:
        raise ValueError(f"Account not found: {resolved_name}")
    cached = state["accounts"][resolved_name].get("identity")
    if isinstance(cached, dict) and cached.get("account_name"):
        return resolved_name, cached
    return resolved_name, refresh_account_identity(paths, resolved_name)


def probe_profile_identity_via_usage(
    source_dir: Path,
    agy_binary: str | None = None,
    timeout_seconds: int = 30,
    live_dir: Path | None = None,
) -> dict:
    resolved_binary = resolve_agy_binary(agy_binary)
    source_home = _resolve_home_source(source_dir)
    profile_source = _resolve_profile_source(source_dir)
    if not profile_has_login_artifacts(profile_source):
        raise ValueError(f"Profile source is missing required auth files: {profile_source}")
    # Each saved account is already a complete home. Probing it directly keeps
    # the shared live home and the manager runtime untouched during switches.
    del live_dir
    env = os.environ.copy()
    env["HOME"] = str(source_home)
    env["PATH"] = env.get("PATH", "/bin:/usr/bin:/usr/local/bin")
    if sys.platform.startswith("linux"):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/tmp/agy-cli-manager-isolated-no-keyring"

    proc = subprocess.run(
        [resolved_binary, "-p", "/usage"],
        cwd=source_home,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    if proc.returncode != 0:
        tail = "\n".join(output.splitlines()[-8:]) if output else "no output"
        raise ValueError(f"agy /usage failed with exit code {proc.returncode}: {tail}")

    match = EMAIL_PATTERN.search(output)
    if match:
        return {
            "account_name": match.group(0),
            "source": "agy:/usage",
        }
    return {
        "account_name": None,
        "source": "agy:/usage",
        "raw_hint": "\n".join(output.splitlines()[:8]),
    }


def resolve_login_profile_identity(
    source_dir: Path,
    agy_binary: str | None = None,
    live_dir: Path | None = None,
) -> dict:
    identity = _best_effort_saved_profile_identity(source_dir)
    if identity.get("account_name"):
        return identity
    try:
        probe = probe_profile_identity_via_usage(
            source_dir,
            agy_binary=agy_binary,
            timeout_seconds=30,
            live_dir=live_dir,
        )
    except (ValueError, subprocess.TimeoutExpired):
        return identity
    if probe.get("account_name"):
        return probe
    return identity


def profile_has_login_artifacts(profile_dir: Path) -> bool:
    return any(
        all((profile_dir / name).is_file() for name in artifact_set)
        for artifact_set in LOGIN_ARTIFACT_SETS
    )


def _derive_health_status(paths: ManagerPaths, name: str, meta: dict) -> str:
    if not meta.get("enabled", True):
        return "disabled"
    cooldown_until = parse_timestamp(meta.get("cooldown_until"))
    if cooldown_until and cooldown_until > utc_now():
        return "cooldown"
    account_path = account_dir(paths, name)
    profile_source = _resolve_profile_source(account_path)
    if not profile_has_login_artifacts(profile_source):
        return "auth_missing"
    try:
        source_home = _resolve_home_source(account_path)
        _extract_access_token(source_home)
        if _token_expiry_due(source_home):
            if not _has_refresh_token(source_home):
                return "auth_expired"
            return "stale"
    except ValueError:
        pass
    if meta.get("last_live_check_error"):
        return "refresh_failed"
    next_live_check_at = parse_timestamp(meta.get("next_live_check_at"))
    if next_live_check_at and next_live_check_at <= utc_now():
        return "stale"
    if meta.get("last_live_check_at"):
        return "healthy"
    return "ready"


def verify_account(paths: ManagerPaths, name: str, meta: dict) -> dict:
    account_path = account_dir(paths, name)
    profile_source = _resolve_profile_source(account_path)
    source_home = _resolve_home_source(account_path)
    enabled = bool(meta.get("enabled", True))
    cooldown_until = parse_timestamp(meta.get("cooldown_until"))
    has_artifacts = profile_has_login_artifacts(profile_source)
    has_access_token = False
    has_refresh_token = False
    access_token_expired = False

    if has_artifacts:
        try:
            _extract_access_token(source_home)
            has_access_token = True
        except ValueError:
            has_access_token = False
        try:
            has_refresh_token = _has_refresh_token(source_home)
        except ValueError:
            has_refresh_token = False
        try:
            access_token_expired = _token_expiry_due(source_home)
        except ValueError:
            access_token_expired = False

    health_status = _derive_health_status(paths, name, meta)
    problem_status = "ok"
    recommended_action = "none"
    summary = "Ready for use."

    if not enabled:
        problem_status = "disabled"
        recommended_action = "enable"
        summary = "Account is disabled."
    elif cooldown_until and cooldown_until > utc_now():
        problem_status = "cooldown"
        recommended_action = "wait"
        summary = f"Account is in cooldown until {cooldown_until.isoformat()}."
    elif not has_artifacts:
        problem_status = "missing_auth"
        recommended_action = "relogin"
        summary = "Managed auth files are missing."
    elif health_status == "auth_expired" or (has_access_token and access_token_expired and not has_refresh_token):
        problem_status = "logged_out"
        recommended_action = "relogin"
        summary = "Access token is expired and no refresh token is available."
    elif meta.get("last_live_check_error"):
        problem_status = "refresh_failed"
        recommended_action = "refresh"
        summary = f"Last live check failed: {meta.get('last_live_check_error')}"
    elif health_status == "stale":
        problem_status = "stale"
        recommended_action = "refresh"
        summary = "Cached live status is stale; refresh is recommended."

    return {
        "name": name,
        "problem_status": problem_status,
        "recommended_action": recommended_action,
        "summary": summary,
        "health_status": health_status,
        "enabled": enabled,
        "has_login_artifacts": has_artifacts,
        "has_access_token": has_access_token,
        "has_refresh_token": has_refresh_token,
        "access_token_expired": access_token_expired,
        "cooldown_until": meta.get("cooldown_until"),
        "last_live_check_error": meta.get("last_live_check_error"),
        "proxy": _normalize_proxy_config(meta.get("proxy")),
    }


def verify_accounts(paths: ManagerPaths) -> dict:
    state = sync_state_from_disk(paths, load_state(paths))
    accounts = {}
    for name, meta in sorted(state["accounts"].items()):
        accounts[name] = verify_account(paths, name, meta)
    return {
        "active": state.get("active"),
        "switch_mode": get_switch_mode(state),
        "accounts": accounts,
    }


def sync_state_from_disk(paths: ManagerPaths, state: dict) -> dict:
    disk_accounts = {p.name for p in paths.accounts_dir.iterdir() if p.is_dir()}
    tracked = state["accounts"]

    for name in sorted(disk_accounts):
        account_path = paths.accounts_dir / name
        try:
            created_at = datetime.fromtimestamp(account_path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            created_at = utc_now().isoformat()
        tracked.setdefault(
            name,
            {
                "enabled": True,
                "status": "standby",
                "last_error": None,
                "cooldown_until": None,
                "fail_count": 0,
                "created_at": created_at,
                "usage_windows": _default_usage_windows(),
                "usage_status": "unknown",
                "usage_value": None,
                "reset_at": None,
                "health_status": "unknown",
                "last_live_check_at": None,
                "last_live_check_error": None,
                "refresh_fail_count": 0,
                "next_live_check_at": None,
                "refresh_policy_seconds": DEFAULT_REFRESH_POLICY_SECONDS,
                "proxy": _default_proxy_config(),
            },
        )
        meta = tracked[name]
        meta.setdefault("created_at", created_at)
        meta.setdefault("usage_windows", _default_usage_windows())
        meta.setdefault("health_status", "unknown")
        meta.setdefault("last_live_check_at", None)
        meta.setdefault("last_live_check_error", None)
        meta.setdefault("refresh_fail_count", 0)
        meta.setdefault("next_live_check_at", None)
        meta.setdefault("refresh_policy_seconds", DEFAULT_REFRESH_POLICY_SECONDS)
        meta["proxy"] = _normalize_proxy_config(meta.get("proxy"))
        _sync_legacy_usage_fields(meta)
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


def save_account_profile(paths: ManagerPaths, name: str, source_dir: Path, overwrite: bool = False) -> None:
    if not name.strip():
        raise ValueError("Account name cannot be empty.")
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Source directory does not exist: {source_dir}")

    home_source = _resolve_home_source(source_dir)
    profile_source = _resolve_profile_source(source_dir)
    if not profile_source.exists() or not profile_source.is_dir():
        raise ValueError(f"Usable profile source not found in {source_dir}")
    if not profile_has_login_artifacts(profile_source):
        raise ValueError(f"Profile source is missing required auth files: {profile_source}")

    target = account_dir(paths, name)
    target_exists = target.exists()
    if target_exists and not overwrite:
        raise ValueError(f"Account already exists: {name}")
    if target_exists:
        _clear_directory(target)
    else:
        target.mkdir(parents=True, exist_ok=False)
    _copy_account_profile(home_source, target)
    identity = _best_effort_saved_profile_identity(target)

    with manager_lock(paths):
        state = load_state(paths)
        state = sync_state_from_disk(paths, state)
        previous_meta = state["accounts"].get(name, {})
        state["accounts"][name] = {
            "enabled": previous_meta.get("enabled", True),
            "status": previous_meta.get("status", "standby"),
            "last_error": None if overwrite else previous_meta.get("last_error"),
            "cooldown_until": None if overwrite else previous_meta.get("cooldown_until"),
            "fail_count": 0 if overwrite else previous_meta.get("fail_count", 0),
            "refresh_fail_count": 0 if overwrite else previous_meta.get("refresh_fail_count", 0),
            "created_at": previous_meta.get("created_at") or utc_now().isoformat(),
            "usage_families": _normalize_usage_families(previous_meta),
            "usage_windows": _normalize_usage_windows(previous_meta),
            "usage_status": previous_meta.get("usage_status", "unknown"),
            "usage_value": previous_meta.get("usage_value"),
            "reset_at": previous_meta.get("reset_at"),
            "health_status": previous_meta.get("health_status", "unknown"),
            "last_live_check_at": previous_meta.get("last_live_check_at"),
            "last_live_check_error": previous_meta.get("last_live_check_error"),
            "next_live_check_at": previous_meta.get("next_live_check_at"),
            "refresh_policy_seconds": int(previous_meta.get("refresh_policy_seconds", DEFAULT_REFRESH_POLICY_SECONDS) or DEFAULT_REFRESH_POLICY_SECONDS),
            "identity": identity,
            "proxy": _normalize_proxy_config(previous_meta.get("proxy")),
            "family_cooldowns": dict(previous_meta.get("family_cooldowns", {}))
            if isinstance(previous_meta.get("family_cooldowns"), dict)
            else {},
        }
        _sync_legacy_usage_fields(state["accounts"][name])
        if overwrite and state.get("active") == name:
            state = _activate_account_locked(paths, state, name)
        if not state.get("active"):
            try:
                state = _activate_account_locked(paths, state, name)
            except BaseException:
                state["accounts"].pop(name, None)
                if not target_exists:
                    shutil.rmtree(target, ignore_errors=True)
                raise
            save_state(paths, state)
        else:
            save_state(paths, state)


def add_account(paths: ManagerPaths, name: str, source_dir: Path) -> None:
    save_account_profile(paths, name, source_dir, overwrite=False)


def import_current(paths: ManagerPaths, name: str, source_dir: Path | None = None) -> None:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        live_dir = source_dir or get_live_dir(state)
        if live_dir is None:
            raise ValueError("No source_dir provided and no live_dir configured.")
        profile_source = _resolve_profile_source(live_dir)
        configured_live = get_live_dir(state)
        is_live_source = configured_live is not None and profile_source.resolve() == configured_live.resolve()
        if not profile_has_login_artifacts(profile_source) and is_live_source:
            _capture_linux_live_credential(profile_source, state)
    add_account(paths, name, live_dir)


def _copy_active_runtime(paths: ManagerPaths, name: str) -> None:
    src = account_dir(paths, name)
    if not src.exists():
        raise ValueError(f"Account not found: {name}")
    if not profile_has_login_artifacts(_resolve_profile_source(src)):
        raise ValueError(f"Account {name} is missing required auth files")

    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    _copy_account_profile(src, paths.runtime_dir)


def _sync_runtime_to_live_dir(paths: ManagerPaths, state: dict) -> None:
    live_dir = get_live_dir(state)
    if live_dir is None:
        return
    _copy_account_profile(paths.runtime_dir, live_dir.parent)


def _activate_account_locked(paths: ManagerPaths, state: dict, name: str) -> dict:
    source_home = account_dir(paths, name)
    source_profile = _resolve_profile_source(source_home)
    if not source_home.exists() or not profile_has_login_artifacts(source_profile):
        raise ValueError(f"Account {name} is missing required auth files")

    live_dir = get_live_dir(state)
    backend = _effective_credential_backend(state)
    if backend == "secret-service" and _agy_processes_running():
        raise ValueError(
            "Refusing to switch the Linux Secret Service credential while agy is running. "
            "Exit agy and retry; the next chatbox worker will coordinate this automatically."
        )

    source_token = source_profile / MANAGED_PROFILE_FILES[0]
    live_token = live_dir / MANAGED_PROFILE_FILES[0] if live_dir is not None else None
    runtime_token = paths.runtime_dir / ".gemini" / MANAGED_PROFILE_FILES[0]
    live_snapshot = _snapshot_file(live_token) if live_token is not None else None
    runtime_snapshot = _snapshot_file(runtime_token)
    secret_snapshot = None

    if backend == "secret-service" and live_dir is not None:
        from agy_cli_manager.credential_store import (
            read_linux_live_credential,
            validate_antigravity_credential,
        )

        source_blob = validate_antigravity_credential(source_token.read_bytes())
        secret_snapshot = read_linux_live_credential(required=False)
    else:
        source_blob = None

    try:
        if source_blob is not None:
            from agy_cli_manager.credential_store import write_linux_live_credential

            write_linux_live_credential(source_blob)
        if live_dir is not None:
            _copy_account_profile(source_home, live_dir.parent)
        _copy_account_profile(source_home, paths.runtime_dir)
    except BaseException:
        if backend == "secret-service" and live_dir is not None:
            from agy_cli_manager.credential_store import restore_linux_live_credential

            try:
                restore_linux_live_credential(secret_snapshot)
            except BaseException:
                pass
        if live_token is not None and live_snapshot is not None:
            _restore_file(live_token, live_snapshot)
        _restore_file(runtime_token, runtime_snapshot)
        raise

    state["active"] = name
    return sync_state_from_disk(paths, state)


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
        state = _activate_account_locked(paths, state, name)
        save_state(paths, state)
        return previous or ""


def switch_next(paths: ManagerPaths) -> str:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        candidates = _eligible_switch_candidates(state)
        if not candidates:
            raise ValueError("No enabled non-cooldown accounts available.")

        current = state.get("active")
        target = _best_switch_candidate(paths, state, exclude=current)
        if target is None and current in candidates and len(candidates) == 1:
            target = current
        if target is None:
            raise ValueError("No eligible standby account is available.")
        if len(candidates) == 1 and current == target:
            raise ValueError("Only one eligible account is available.")
        state = _activate_account_locked(paths, state, target)
        save_state(paths, state)
        return target


def get_status_snapshot(paths: ManagerPaths) -> dict:
    state = sync_state_from_disk(paths, load_state(paths))
    snapshot_accounts = {}
    for name, meta in sorted(state["accounts"].items()):
        derived_health_status = _derive_health_status(paths, name, meta)
        snapshot_accounts[name] = {
            "enabled": bool(meta.get("enabled", True)),
            "status": meta.get("status", "standby"),
            "last_error": meta.get("last_error"),
            "cooldown_until": meta.get("cooldown_until"),
            "fail_count": int(meta.get("fail_count", 0) or 0),
            "refresh_fail_count": int(meta.get("refresh_fail_count", 0) or 0),
            "created_at": meta.get("created_at"),
            "usage_families": _normalize_usage_families(meta),
            "usage_windows": _normalize_usage_windows(meta),
            "usage_status": meta.get("usage_status", "unknown"),
            "usage_value": meta.get("usage_value"),
            "reset_at": meta.get("reset_at"),
            "health_status": derived_health_status,
            "stored_health_status": meta.get("health_status", "unknown"),
            "last_live_check_at": meta.get("last_live_check_at"),
            "last_live_check_error": meta.get("last_live_check_error"),
            "next_live_check_at": meta.get("next_live_check_at"),
            "refresh_policy_seconds": int(meta.get("refresh_policy_seconds", DEFAULT_REFRESH_POLICY_SECONDS) or DEFAULT_REFRESH_POLICY_SECONDS),
            "identity": meta.get("identity") if isinstance(meta.get("identity"), dict) else None,
            "proxy": _normalize_proxy_config(meta.get("proxy")),
            "family_cooldowns": dict(meta.get("family_cooldowns", {}))
            if isinstance(meta.get("family_cooldowns"), dict)
            else {},
        }
    active_name = state.get("active")
    active_meta = state["accounts"].get(active_name) if active_name else None
    return {
        "root": str(paths.root),
        "runtime_dir": str(paths.runtime_dir),
        "lock_file": str(paths.lock_file),
        "live_dir": state.get("live_dir"),
        "credential_backend": _configured_credential_backend(state),
        "active": active_name,
        "active_proxy": _normalize_proxy_config(active_meta.get("proxy")) if isinstance(active_meta, dict) else _default_proxy_config(),
        "switch_mode": get_switch_mode(state),
        "switch_policy": _state_switch_policy(state),
        "switch_runtime": _normalize_switch_runtime(state.get("switch_runtime")),
        "switch_history": _normalize_switch_history(state.get("switch_history")),
        "log_watch": get_log_watch_snapshot(paths),
        "accounts": snapshot_accounts,
    }


def get_switch_policy(paths: ManagerPaths) -> dict:
    state = sync_state_from_disk(paths, load_state(paths))
    return dict(_state_switch_policy(state))


def get_credential_status(paths: ManagerPaths) -> dict:
    state = sync_state_from_disk(paths, load_state(paths))
    configured = _configured_credential_backend(state)
    from agy_cli_manager.credential_store import linux_secret_service_available

    available = linux_secret_service_available()
    effective = "file"
    error = None
    try:
        effective = _effective_credential_backend(state)
    except ValueError as exc:
        error = str(exc)
    return {
        "configured": configured,
        "effective": effective,
        "secret_service_available": available,
        "error": error,
    }


def set_credential_backend(paths: ManagerPaths, backend: str) -> dict:
    normalized = _normalize_credential_backend(backend)
    if normalized != backend.strip().lower():
        raise ValueError(f"Unsupported credential backend: {backend}")
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        state["credential_backend"] = normalized
        save_state(paths, state)
    return get_credential_status(paths)


def capture_active_credential(paths: ManagerPaths) -> str:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        active = state.get("active")
        if not active:
            raise ValueError("No active account is set.")
        if _effective_credential_backend(state) != "secret-service":
            raise ValueError("The effective credential backend is not secret-service.")
        if _agy_processes_running():
            raise ValueError("Refusing to capture credentials while agy is running.")
        target_profile = _resolve_profile_source(account_dir(paths, active))
        _capture_linux_live_credential(target_profile, state)
        _copy_account_profile(account_dir(paths, active), paths.runtime_dir)
        return active


def get_account_proxy(paths: ManagerPaths, name: str | None = None) -> tuple[str, dict]:
    state = sync_state_from_disk(paths, load_state(paths))
    resolved_name = name or state.get("active")
    if not resolved_name:
        raise ValueError("No active account.")
    meta = state["accounts"].get(resolved_name)
    if meta is None:
        raise ValueError(f"Unknown account: {resolved_name}")
    return resolved_name, _normalize_proxy_config(meta.get("proxy"))


def list_account_proxies(paths: ManagerPaths) -> dict:
    state = sync_state_from_disk(paths, load_state(paths))
    accounts = {}
    for name, meta in sorted(state["accounts"].items()):
        accounts[name] = {
            "active": name == state.get("active"),
            "status": meta.get("status", "standby"),
            "enabled": bool(meta.get("enabled", True)),
            "proxy": _normalize_proxy_config(meta.get("proxy")),
        }
    return {
        "active": state.get("active"),
        "accounts": accounts,
    }


def set_account_proxy(
    paths: ManagerPaths,
    name: str,
    *,
    url: str,
    label: str | None = None,
    enabled: bool = True,
) -> dict:
    proxy_url = str(url).strip()
    if not proxy_url:
        raise ValueError("Proxy URL cannot be empty.")
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(name)
        if meta is None:
            raise ValueError(f"Unknown account: {name}")
        meta["proxy"] = _normalize_proxy_config(
            {
                "enabled": enabled,
                "url": proxy_url,
                "label": label,
            }
        )
        save_state(paths, state)
        return dict(meta["proxy"])


def clear_account_proxy(paths: ManagerPaths, name: str) -> None:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(name)
        if meta is None:
            raise ValueError(f"Unknown account: {name}")
        meta["proxy"] = _default_proxy_config()
        save_state(paths, state)


def set_switch_mode(paths: ManagerPaths, mode: str) -> str:
    normalized = _normalize_switch_mode(mode)
    if normalized != mode.strip().lower():
        raise ValueError(f"Unsupported switch mode: {mode}")
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        state["switch_mode"] = normalized
        save_state(paths, state)
        return normalized


def update_switch_policy(
    paths: ManagerPaths,
    *,
    short_usage_threshold_percent: float | None = None,
    gemini_usage_threshold_percent: float | None = None,
    other_usage_threshold_percent: float | None = None,
    refresh_failure_threshold: int | None = None,
    candidate_strategy: str | None = None,
    family_fallback_strategy: str | None = None,
) -> dict:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        policy = _state_switch_policy(state)
        if short_usage_threshold_percent is not None:
            value = float(short_usage_threshold_percent)
            if value < 0.0 or value > 100.0:
                raise ValueError("short_usage_threshold_percent must be between 0 and 100.")
            policy["short_usage_threshold_percent"] = value
            policy["family_thresholds"] = {family: value for family in USAGE_FAMILY_NAMES}
        for family, requested_value in (
            ("gemini", gemini_usage_threshold_percent),
            ("other", other_usage_threshold_percent),
        ):
            if requested_value is None:
                continue
            value = float(requested_value)
            if value < 0.0 or value > 100.0:
                raise ValueError(f"{family}_usage_threshold_percent must be between 0 and 100.")
            policy["family_thresholds"][family] = value
        if refresh_failure_threshold is not None:
            value = int(refresh_failure_threshold)
            if value < 1:
                raise ValueError("refresh_failure_threshold must be at least 1.")
            policy["refresh_failure_threshold"] = value
        if candidate_strategy is not None:
            normalized_strategy = _normalize_candidate_strategy(candidate_strategy)
            if normalized_strategy != candidate_strategy.strip().lower():
                raise ValueError(f"Unsupported candidate strategy: {candidate_strategy}")
            policy["candidate_strategy"] = normalized_strategy
        if family_fallback_strategy is not None:
            normalized_fallback = _normalize_family_fallback_strategy(family_fallback_strategy)
            if normalized_fallback != family_fallback_strategy.strip().lower():
                raise ValueError(f"Unsupported family fallback strategy: {family_fallback_strategy}")
            policy["family_fallback_strategy"] = normalized_fallback
        state["switch_policy"] = policy
        save_state(paths, state)
        return dict(policy)


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
        meta["refresh_fail_count"] = 0
        meta["last_live_check_error"] = None
        state = sync_state_from_disk(paths, state)
        save_state(paths, state)


def update_account_runtime_metadata(
    paths: ManagerPaths,
    name: str,
    *,
    usage_status: str | None = None,
    usage_value: str | int | float | None = None,
    reset_at: datetime | str | None = None,
    short_usage_status: str | None = None,
    short_usage_value: str | int | float | None = None,
    short_reset_at: datetime | str | None = None,
    weekly_usage_status: str | None = None,
    weekly_usage_value: str | int | float | None = None,
    weekly_reset_at: datetime | str | None = None,
    health_status: str | None = None,
    last_live_check_at: datetime | str | None = None,
    last_live_check_error: str | None = None,
    next_live_check_at: datetime | str | None = None,
    refresh_policy_seconds: int | None = None,
) -> dict:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        meta = state["accounts"].get(name)
        if meta is None:
            raise ValueError(f"Account not found: {name}")
        windows = _normalize_usage_windows(meta)
        if usage_status is not None:
            windows["short"]["status"] = usage_status
        if usage_value is not None:
            windows["short"]["value"] = usage_value
        if reset_at is not None:
            windows["short"]["reset_at"] = _normalize_timestamp(reset_at)
        if short_usage_status is not None:
            windows["short"]["status"] = short_usage_status
        if short_usage_value is not None:
            windows["short"]["value"] = short_usage_value
        if short_reset_at is not None:
            windows["short"]["reset_at"] = _normalize_timestamp(short_reset_at)
        if weekly_usage_status is not None:
            windows["weekly"]["status"] = weekly_usage_status
        if weekly_usage_value is not None:
            windows["weekly"]["value"] = weekly_usage_value
        if weekly_reset_at is not None:
            windows["weekly"]["reset_at"] = _normalize_timestamp(weekly_reset_at)
        meta["usage_windows"] = windows
        _sync_legacy_usage_fields(meta)
        if health_status is not None:
            meta["health_status"] = health_status
        if last_live_check_at is not None:
            meta["last_live_check_at"] = _normalize_timestamp(last_live_check_at)
        if last_live_check_error is not None:
            meta["last_live_check_error"] = last_live_check_error
        if next_live_check_at is not None:
            meta["next_live_check_at"] = _normalize_timestamp(next_live_check_at)
        if refresh_policy_seconds is not None:
            if refresh_policy_seconds <= 0:
                raise ValueError("refresh_policy_seconds must be positive.")
            meta["refresh_policy_seconds"] = int(refresh_policy_seconds)
        save_state(paths, state)
        return get_status_snapshot(paths)["accounts"][name]


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
        state = _activate_account_locked(paths, state, active)
        save_state(paths, state)
        return active


def rotate_after_failure(
    paths: ManagerPaths,
    reason: str,
    cooldown_minutes: int = 60,
    live_dir: Path | None = None,
    force_switch: bool = False,
    dedupe_seconds: int = DEFAULT_SWITCH_DEDUPE_SECONDS,
    trigger: str = "unknown",
    request_id: str | None = None,
    required_family: str | None = None,
) -> RotationResult:
    if cooldown_minutes < 0:
        raise ValueError("Cooldown minutes must be non-negative.")

    with manager_lock(paths):
        return rotate_after_failure_locked(
            paths,
            reason,
            cooldown_minutes=cooldown_minutes,
            live_dir=live_dir,
            force_switch=force_switch,
            dedupe_seconds=dedupe_seconds,
            trigger=trigger,
            request_id=request_id,
            required_family=required_family,
        )


def rotate_after_failure_locked(
    paths: ManagerPaths,
    reason: str,
    cooldown_minutes: int = 60,
    live_dir: Path | None = None,
    force_switch: bool = False,
    dedupe_seconds: int = DEFAULT_SWITCH_DEDUPE_SECONDS,
    trigger: str = "unknown",
    request_id: str | None = None,
    required_family: str | None = None,
) -> RotationResult:
    if cooldown_minutes < 0:
        raise ValueError("Cooldown minutes must be non-negative.")

    family = _normalize_usage_family(required_family, allow_none=True)
    state = sync_state_from_disk(paths, load_state(paths))
    if live_dir is not None:
        state["live_dir"] = str(live_dir.resolve())
    switch_mode = get_switch_mode(state)
    runtime = _normalize_switch_runtime(state.get("switch_runtime"))
    now = utc_now()
    now_iso = now.isoformat()

    last_completed_at = parse_timestamp(runtime.get("last_completed_at"))
    if (
        dedupe_seconds > 0
        and runtime.get("status") == "ready"
        and runtime.get("reason") == reason
        and runtime.get("required_family") == family
        and last_completed_at is not None
        and (now - last_completed_at).total_seconds() <= dedupe_seconds
        and state.get("active")
    ):
        _mark_switch_runtime(
            state,
            status="ready",
            reason=reason,
            trigger=trigger,
            request_id=request_id,
            required_family=family,
            active=state.get("active"),
            previous_active=runtime.get("previous_active"),
            started_at=runtime.get("last_started_at"),
            completed_at=runtime.get("last_completed_at"),
        )
        _append_switch_history(
            state,
            reason=reason,
            trigger=trigger,
            request_id=request_id,
            previous_active=runtime.get("previous_active"),
            active=state.get("active"),
            switched_to=None,
            outcome="already_switched",
            cooldown_minutes=0,
            required_family=family,
            at=runtime.get("last_completed_at"),
        )
        save_state(paths, state)
        return RotationResult(
            previous_active=runtime.get("previous_active"),
            active=state.get("active"),
            switched_to=None,
            marked_bad=False,
            reason=reason,
            cooldown_minutes=0,
            outcome="already_switched",
        )

    previous = state.get("active")
    _mark_switch_runtime(
        state,
        status="switching",
        reason=reason,
        trigger=trigger,
        request_id=request_id,
        required_family=family,
        active=previous,
        previous_active=previous,
        started_at=now_iso,
        completed_at=None,
    )
    save_state(paths, state)
    if not previous:
        _mark_switch_runtime(
            state,
            status="no_account",
            reason=reason,
            trigger=trigger,
            request_id=request_id,
            required_family=family,
            active=None,
            previous_active=None,
            completed_at=utc_now().isoformat(),
        )
        _append_switch_history(
            state,
            reason=reason,
            trigger=trigger,
            request_id=request_id,
            previous_active=None,
            active=None,
            switched_to=None,
            outcome="no_active",
            cooldown_minutes=cooldown_minutes,
            required_family=family,
        )
        save_state(paths, state)
        return RotationResult(
            previous_active=None,
            active=None,
            switched_to=None,
            marked_bad=False,
            reason=reason,
            cooldown_minutes=cooldown_minutes,
            outcome="no_active",
        )

    meta = state["accounts"].get(previous)
    if meta is None:
        state["active"] = None
        _mark_switch_runtime(
            state,
            status="no_account",
            reason=reason,
            trigger=trigger,
            request_id=request_id,
            required_family=family,
            active=None,
            previous_active=previous,
            completed_at=utc_now().isoformat(),
        )
        _append_switch_history(
            state,
            reason=reason,
            trigger=trigger,
            request_id=request_id,
            previous_active=previous,
            active=None,
            switched_to=None,
            outcome="active_missing",
            cooldown_minutes=cooldown_minutes,
            required_family=family,
        )
        save_state(paths, state)
        return RotationResult(
            previous_active=previous,
            active=None,
            switched_to=None,
            marked_bad=False,
            reason=reason,
            cooldown_minutes=cooldown_minutes,
            outcome="active_missing",
        )

    meta["last_error"] = reason
    meta["fail_count"] = int(meta.get("fail_count", 0)) + 1
    if family is not None:
        family_cooldowns = meta.get("family_cooldowns")
        if not isinstance(family_cooldowns, dict):
            family_cooldowns = {}
        family_cooldowns[family] = (
            (utc_now() + timedelta(minutes=cooldown_minutes)).isoformat()
            if cooldown_minutes > 0
            else None
        )
        meta["family_cooldowns"] = family_cooldowns
        meta["cooldown_until"] = None
    elif cooldown_minutes > 0:
        meta["cooldown_until"] = (utc_now() + timedelta(minutes=cooldown_minutes)).isoformat()
    else:
        meta["cooldown_until"] = None
    state["active"] = None
    state = sync_state_from_disk(paths, state)

    switched_to = None
    if force_switch or switch_mode == "auto":
        switched_to = _best_switch_candidate(paths, state, exclude=previous, required_family=family)
        if switched_to:
            state = _activate_account_locked(paths, state, switched_to)

    _mark_switch_runtime(
        state,
        status="ready" if state.get("active") else "no_account",
        reason=reason,
        trigger=trigger,
        request_id=request_id,
        required_family=family,
        active=state.get("active"),
        previous_active=previous,
        completed_at=utc_now().isoformat(),
    )
    _append_switch_history(
        state,
        reason=reason,
        trigger=trigger,
        request_id=request_id,
        previous_active=previous,
        active=state.get("active"),
        switched_to=switched_to,
        outcome="switched" if switched_to else "no_candidate",
        cooldown_minutes=cooldown_minutes,
        required_family=family,
    )
    save_state(paths, state)
    return RotationResult(
        previous_active=previous,
        active=state.get("active"),
        switched_to=switched_to,
        marked_bad=True,
        reason=reason,
        cooldown_minutes=cooldown_minutes,
        outcome="switched" if switched_to else "no_candidate",
    )


def _run_interactive_agy_login(runtime_home: Path, resolved_binary: str, timeout_seconds: int) -> None:
    env = os.environ.copy()
    env["HOME"] = str(runtime_home)
    env["PATH"] = env.get("PATH", "/bin:/usr/bin:/usr/local/bin")
    try:
        proc = subprocess.Popen(
            [resolved_binary],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            cwd=runtime_home,
            env=env,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"agy binary not found: {resolved_binary}") from exc

    start_time = time.time()
    print("Launching real agy login session.")
    print("Complete onboarding/login there, then exit agy to save the profile.")
    sys.stdout.flush()
    try:
        while True:
            if proc.poll() is not None:
                break
            if time.time() - start_time > timeout_seconds:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise ValueError(f"Login timed out after {timeout_seconds} seconds.")
            time.sleep(0.2)
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise


def login_account(
    paths: ManagerPaths,
    name: str,
    agy_binary: str | None,
    timeout_seconds: int = 600,
) -> str | None:
    if not name.strip():
        raise ValueError("Account name cannot be empty.")
    if not os.isatty(sys.stdin.fileno()):
        raise ValueError("Interactive login requires a TTY.")

    resolved_binary = resolve_agy_binary(agy_binary)
    ensure_layout(paths)
    with manager_lock(paths):
        login_state = sync_state_from_disk(paths, load_state(paths))
    with tempfile.TemporaryDirectory(prefix="login-", dir=paths.root) as home_string:
        runtime_home = Path(home_string)
        login_dir = runtime_home / ".gemini"
        login_dir.mkdir()

        with _isolated_linux_login_credential(login_state, login_dir):
            _run_interactive_agy_login(runtime_home, resolved_binary, timeout_seconds)

        if not profile_has_login_artifacts(login_dir):
            raise ValueError("agy login did not produce a usable auth profile.")

        identity = resolve_login_profile_identity(login_dir, agy_binary=resolved_binary, live_dir=login_dir)
        detected_name = identity.get("account_name")
        # The caller's name is the stable profile label. Keep detected identity
        # as metadata so multiple profiles never collapse onto one directory.
        storage_name = normalize_account_storage_name(name)
        if detected_name and storage_name != name:
            print(f"detected-account: {detected_name}")
            print(f"storage-name: {storage_name}")

        overwrite = False
        if account_dir(paths, storage_name).exists():
            prompt = f"Account '{storage_name}' already exists. Overwrite it? [y/N]: "
            answer = input(prompt).strip().lower()
            if answer not in {"y", "yes"}:
                storage_name = next_available_account_name(paths, storage_name)
                print(f"saving-as: {storage_name}")
            else:
                overwrite = True

        save_account_profile(paths, storage_name, runtime_home, overwrite=overwrite)
        return storage_name


def format_status(paths: ManagerPaths) -> str:
    state = sync_state_from_disk(paths, load_state(paths))
    switch_runtime = _normalize_switch_runtime(state.get("switch_runtime"))
    switch_history = _normalize_switch_history(state.get("switch_history"))
    lines = [
        f"root: {paths.root}",
        f"runtime: {paths.runtime_dir}",
        f"lock: {paths.lock_file}",
        f"live_dir: {state.get('live_dir') or '-'}",
        f"active: {state.get('active') or '-'}",
        f"active_proxy: {_normalize_proxy_config(state['accounts'].get(state.get('active'), {}).get('proxy') if state.get('active') else None).get('label') or (_normalize_proxy_config(state['accounts'].get(state.get('active'), {}).get('proxy') if state.get('active') else None).get('url') or '-')}",
        f"switch_mode: {get_switch_mode(state)}",
        (
            "switch_runtime: "
            f"{switch_runtime.get('status') or 'idle'}"
            f" reason={switch_runtime.get('reason') or '-'}"
            f" trigger={switch_runtime.get('trigger') or '-'}"
            f" active={switch_runtime.get('active') or '-'}"
            f" previous={switch_runtime.get('previous_active') or '-'}"
        ),
        (
            "last_switch: "
            f"{(switch_history[-1].get('outcome') if switch_history else '-')}"
            f" reason={(switch_history[-1].get('reason') if switch_history else '-')}"
            f" trigger={(switch_history[-1].get('trigger') if switch_history else '-')}"
            f" at={(switch_history[-1].get('at') if switch_history else '-')}"
        ),
        "accounts:",
    ]
    for name, meta in sorted(state["accounts"].items()):
        flag = "enabled" if meta.get("enabled", True) else "disabled"
        extra = []
        identity = meta.get("identity")
        if isinstance(identity, dict) and identity.get("account_name"):
            extra.append(f"account_name={identity['account_name']}")
            if identity.get("source"):
                extra.append(f"identity_source={identity['source']}")
        if meta.get("cooldown_until"):
            extra.append(f"cooldown_until={meta['cooldown_until']}")
        if meta.get("fail_count"):
            extra.append(f"fail_count={meta['fail_count']}")
        if meta.get("refresh_fail_count"):
            extra.append(f"refresh_fail_count={meta['refresh_fail_count']}")
        if meta.get("last_error"):
            extra.append(f"last_error={meta['last_error']}")
        proxy = _normalize_proxy_config(meta.get("proxy"))
        if proxy.get("url"):
            extra.append(f"proxy_label={proxy.get('label') or '-'}")
            extra.append(f"proxy_enabled={proxy.get('enabled', False)}")
        suffix = f" [{' ; '.join(extra)}]" if extra else ""
        lines.append(f"  - {name}: {meta.get('status', 'standby')} ({flag}){suffix}")
    if not state["accounts"]:
        lines.append("  - none")
    return "\n".join(lines)

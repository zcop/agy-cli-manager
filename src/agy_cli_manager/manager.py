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

import fcntl


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
DEFAULT_SWITCH_MODE = "auto"
VALID_SWITCH_MODES = ("auto", "manual")
DEFAULT_REFRESH_FAILURE_SWITCH_THRESHOLD = 2
DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT = 10.0
DEFAULT_CANDIDATE_STRATEGY = "balanced"
VALID_CANDIDATE_STRATEGIES = ("balanced", "highest-short", "round-robin")
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
                "switch_mode": DEFAULT_SWITCH_MODE,
                "switch_policy": _default_switch_policy(),
            },
        )


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
    data.setdefault("live_dir", str(default_live_dir()))
    data["switch_mode"] = _normalize_switch_mode(data.get("switch_mode"))
    data["switch_policy"] = _normalize_switch_policy(data.get("switch_policy"))
    if data.get("live_dir") is None:
        data["live_dir"] = str(default_live_dir())
    return data


def save_state(paths: ManagerPaths, state: dict) -> None:
    with paths.state_file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def _normalize_switch_mode(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_SWITCH_MODES:
            return normalized
    return DEFAULT_SWITCH_MODE


def get_switch_mode(state: dict) -> str:
    return _normalize_switch_mode(state.get("switch_mode"))


def _default_switch_policy() -> dict:
    return {
        "short_usage_threshold_percent": DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT,
        "refresh_failure_threshold": DEFAULT_REFRESH_FAILURE_SWITCH_THRESHOLD,
        "candidate_strategy": DEFAULT_CANDIDATE_STRATEGY,
    }


def _normalize_candidate_strategy(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_CANDIDATE_STRATEGIES:
            return normalized
    return DEFAULT_CANDIDATE_STRATEGY


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
        failure_threshold = raw.get("refresh_failure_threshold")
        try:
            if failure_threshold is not None:
                failure_value = int(failure_threshold)
                if failure_value >= 1:
                    policy["refresh_failure_threshold"] = failure_value
        except (TypeError, ValueError):
            pass
        policy["candidate_strategy"] = _normalize_candidate_strategy(raw.get("candidate_strategy"))
    return policy


def _state_switch_policy(state: dict) -> dict:
    return _normalize_switch_policy(state.get("switch_policy"))


def account_dir(paths: ManagerPaths, name: str) -> Path:
    return paths.accounts_dir / name


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


def _normalize_usage_windows(meta: dict) -> dict:
    raw_windows = meta.get("usage_windows")
    windows = _default_usage_windows()
    if isinstance(raw_windows, dict):
        for name in USAGE_WINDOW_NAMES:
            raw = raw_windows.get(name)
            if not isinstance(raw, dict):
                continue
            windows[name] = {
                "status": raw.get("status", "unknown") or "unknown",
                "value": raw.get("value"),
                "reset_at": raw.get("reset_at"),
            }

    short_window = windows["short"]
    if short_window.get("value") is None and meta.get("usage_value") is not None:
        short_window["value"] = meta.get("usage_value")
    if short_window.get("status") == "unknown" and meta.get("usage_status") is not None:
        short_window["status"] = meta.get("usage_status") or "unknown"
    if short_window.get("reset_at") is None and meta.get("reset_at") is not None:
        short_window["reset_at"] = meta.get("reset_at")
    return windows


def _sync_legacy_usage_fields(meta: dict) -> None:
    windows = _normalize_usage_windows(meta)
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


def _parse_quota_windows_from_summary(summary_response: dict) -> tuple[dict, dict, int]:
    group = _select_quota_summary_group(summary_response)
    if not isinstance(group, dict):
        return _default_usage_window(), _default_usage_window(), 0
    buckets = group.get("buckets")
    if not isinstance(buckets, list):
        return _default_usage_window(), _default_usage_window(), 0

    short_window = _default_usage_window()
    weekly_window = _default_usage_window()
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        window_name = bucket.get("window")
        if window_name == "5h":
            short_window = _parse_summary_bucket(bucket)
        elif window_name == "weekly":
            weekly_window = _parse_summary_bucket(bucket)
    return short_window, weekly_window, len(buckets)


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


def _is_short_window_exhausted(meta: dict, now: datetime | None = None, *, threshold_percent: float = DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT) -> bool:
    current = now or utc_now()
    windows = _normalize_usage_windows(meta)
    short = windows.get("short", {})
    if short.get("status") != "known":
        return False
    value = _coerce_usage_value(short.get("value"))
    if value is None or value > threshold_percent:
        return False
    reset_at = parse_timestamp(short.get("reset_at"))
    if reset_at is not None and reset_at <= current:
        return False
    return True


def _cooldown_minutes_from_short_window(meta: dict, now: datetime | None = None) -> int:
    current = now or utc_now()
    windows = _normalize_usage_windows(meta)
    short = windows.get("short", {})
    reset_at = parse_timestamp(short.get("reset_at"))
    if reset_at is None or reset_at <= current:
        return 60
    delta_seconds = max(60.0, (reset_at - current).total_seconds())
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


def _candidate_usage_value(meta: dict, window_name: str) -> float | None:
    windows = _normalize_usage_windows(meta)
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


def _best_switch_candidate(paths: ManagerPaths, state: dict, *, exclude: str | None = None) -> str | None:
    policy = _state_switch_policy(state)
    strategy = policy["candidate_strategy"]
    threshold_percent = float(policy["short_usage_threshold_percent"])
    candidates = _eligible_switch_candidates(state, exclude=exclude)
    if not candidates:
        return None

    ranked: list[tuple[tuple[object, ...], str]] = []
    for name in candidates:
        meta = state["accounts"].get(name)
        if not isinstance(meta, dict):
            continue
        health = _derive_health_status(paths, name, meta)
        if health in {"auth_missing", "auth_expired", "disabled", "cooldown"}:
            continue

        short_value = _candidate_usage_value(meta, "short")
        weekly_value = _candidate_usage_value(meta, "weekly")
        short_known = short_value is not None
        short_low = short_known and short_value <= threshold_percent
        weekly_known = weekly_value is not None

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
                0 if short_known and not short_low else 1,
                str(meta.get("created_at") or ""),
                name.lower(),
            )
        else:
            score = (
                _candidate_health_priority(health),
                0 if short_known and not short_low else 1,
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
        return candidates[0]

    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


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


def ensure_active_account(paths: ManagerPaths, *, force: bool = False) -> EnsureActiveResult:
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
        )

    if not active_name:
        with manager_lock(paths):
            state = sync_state_from_disk(paths, load_state(paths))
            switched_to = _best_switch_candidate(paths, state)
            if switched_to:
                _copy_active_runtime(paths, switched_to)
                state["active"] = switched_to
                state = sync_state_from_disk(paths, state)
                _sync_runtime_to_live_dir(paths, state)
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
            )
        return EnsureActiveResult(
            triggered=True,
            switch_mode=switch_mode,
            previous_active=None,
            active=switched_to,
            switched_to=switched_to,
            reason="no_active_account",
            cooldown_minutes=0,
        )

    active_meta = accounts.get(active_name)
    if not isinstance(active_meta, dict):
        with manager_lock(paths):
            state = sync_state_from_disk(paths, load_state(paths))
            switched_to = _best_switch_candidate(paths, state, exclude=active_name)
            if switched_to:
                _copy_active_runtime(paths, switched_to)
                state["active"] = switched_to
                state = sync_state_from_disk(paths, state)
                _sync_runtime_to_live_dir(paths, state)
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
            )
        return EnsureActiveResult(
            triggered=True,
            switch_mode=switch_mode,
            previous_active=active_name,
            active=switched_to,
            switched_to=switched_to,
            reason="active_missing",
            cooldown_minutes=0,
        )

    reason = None
    cooldown_minutes = 0
    health = active_meta.get("health_status")
    if health in {"auth_missing", "auth_expired"}:
        reason = health
        cooldown_minutes = 60
    elif _is_short_window_exhausted(
        active_meta,
        now,
        threshold_percent=float(switch_policy.get("short_usage_threshold_percent", DEFAULT_SHORT_SWITCH_THRESHOLD_PERCENT)),
    ):
        reason = "quota_exhausted"
        cooldown_minutes = _cooldown_minutes_from_short_window(active_meta, now)
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
        )

    result = rotate_after_failure(
        paths,
        reason=reason,
        cooldown_minutes=cooldown_minutes,
        force_switch=True,
    )
    return EnsureActiveResult(
        triggered=bool(result.switched_to or result.previous_active),
        switch_mode=switch_mode,
        previous_active=result.previous_active,
        active=result.active,
        switched_to=result.switched_to,
        reason=reason,
        cooldown_minutes=cooldown_minutes,
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
        live_dir = get_live_dir(state)
    runtime_home = resolve_runtime_home(live_dir)
    if not profile_has_login_artifacts(_resolve_profile_source(source_home)):
        fallback_home = account_dir(paths, account_name)
        if name is None and profile_has_login_artifacts(_resolve_profile_source(fallback_home)):
            source_home = fallback_home
        else:
            raise ValueError(f"Profile source is missing required auth files: {_resolve_profile_source(source_home)}")

    if name is None:
        models = _run_agy_models_command(source_home, agy_binary=agy_binary, timeout_seconds=timeout_seconds)
        return {
            "account": account_name,
            "source_home": str(source_home),
            "models": models,
            "count": len(models),
        }

    with tempfile.TemporaryDirectory(prefix="agy-models-restore-") as restore_root_str:
        restore_root = Path(restore_root_str)
        restore_home = restore_root / "home"
        _copy_account_profile(runtime_home, restore_home)
        try:
            _copy_account_profile(source_home, runtime_home)
            models = _run_agy_models_command(runtime_home, agy_binary=agy_binary, timeout_seconds=timeout_seconds)
        finally:
            _copy_account_profile(restore_home, runtime_home)
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
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        account_name, source_home = _resolve_usage_refresh_target(paths, state, name)
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
        short_window, weekly_window, bucket_count = _parse_quota_windows_from_summary(quota_response)
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
            bucket_count=bucket_count,
        )

        refreshed_at = utc_now()
        if source_home != account_dir(paths, account_name):
            target_dir = account_dir(paths, account_name)
            if target_dir.exists():
                source_profile = _resolve_profile_source(source_home)
                target_profile = target_dir / ".gemini"
                _copy_managed_profile_files(source_profile, target_profile)
                project_id_file = _project_id_path(source_home)
                if project_id_file.is_file():
                    dst_project_id = _project_id_path(target_dir)
                    dst_project_id.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(project_id_file, dst_project_id)
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
            windows = _normalize_usage_windows(meta)
            windows["short"]["status"] = result.short_usage_status
            windows["short"]["value"] = result.short_usage_value
            windows["short"]["reset_at"] = result.short_reset_at
            windows["weekly"]["status"] = result.weekly_usage_status
            windows["weekly"]["value"] = result.weekly_usage_value
            windows["weekly"]["reset_at"] = result.weekly_reset_at
            meta["usage_windows"] = windows
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
    runtime_home = resolve_runtime_home(live_dir)

    with tempfile.TemporaryDirectory(prefix="agy-usage-restore-") as restore_root_str:
        restore_root = Path(restore_root_str)
        restore_home = restore_root / "home"
        _copy_account_profile(runtime_home, restore_home)
        try:
            _copy_account_profile(source_home, runtime_home)

            env = os.environ.copy()
            env["HOME"] = str(runtime_home)
            env["PATH"] = env.get("PATH", "/bin:/usr/bin:/usr/local/bin")

            proc = subprocess.run(
                [resolved_binary, "-p", "/usage"],
                cwd=runtime_home,
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
        finally:
            _copy_account_profile(restore_home, runtime_home)


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
            return "auth_expired"
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
        }
        _sync_legacy_usage_fields(state["accounts"][name])
        if overwrite and state.get("active") == name:
            _copy_active_runtime(paths, name)
            state = sync_state_from_disk(paths, state)
            _sync_runtime_to_live_dir(paths, state)
        save_state(paths, state)
        if not state.get("active"):
            _copy_active_runtime(paths, name)
            state["active"] = name
            state = sync_state_from_disk(paths, state)
            _sync_runtime_to_live_dir(paths, state)
            save_state(paths, state)


def add_account(paths: ManagerPaths, name: str, source_dir: Path) -> None:
    save_account_profile(paths, name, source_dir, overwrite=False)


def import_current(paths: ManagerPaths, name: str, source_dir: Path | None = None) -> None:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        live_dir = source_dir or get_live_dir(state)
        if live_dir is None:
            raise ValueError("No source_dir provided and no live_dir configured.")
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
        _copy_active_runtime(paths, target)
        state["active"] = target
        state = sync_state_from_disk(paths, state)
        _sync_runtime_to_live_dir(paths, state)
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
        }
    return {
        "root": str(paths.root),
        "runtime_dir": str(paths.runtime_dir),
        "lock_file": str(paths.lock_file),
        "live_dir": state.get("live_dir"),
        "active": state.get("active"),
        "switch_mode": get_switch_mode(state),
        "switch_policy": _state_switch_policy(state),
        "accounts": snapshot_accounts,
    }


def get_switch_policy(paths: ManagerPaths) -> dict:
    state = sync_state_from_disk(paths, load_state(paths))
    return dict(_state_switch_policy(state))


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
    refresh_failure_threshold: int | None = None,
    candidate_strategy: str | None = None,
) -> dict:
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        policy = _state_switch_policy(state)
        if short_usage_threshold_percent is not None:
            value = float(short_usage_threshold_percent)
            if value < 0.0 or value > 100.0:
                raise ValueError("short_usage_threshold_percent must be between 0 and 100.")
            policy["short_usage_threshold_percent"] = value
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
        _copy_active_runtime(paths, active)
        _sync_runtime_to_live_dir(paths, state)
        save_state(paths, state)
        return active


def rotate_after_failure(
    paths: ManagerPaths,
    reason: str,
    cooldown_minutes: int = 60,
    live_dir: Path | None = None,
    force_switch: bool = False,
) -> RotationResult:
    if cooldown_minutes < 0:
        raise ValueError("Cooldown minutes must be non-negative.")

    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        if live_dir is not None:
            state["live_dir"] = str(live_dir.resolve())
        switch_mode = get_switch_mode(state)

        previous = state.get("active")
        if not previous:
            save_state(paths, state)
            return RotationResult(
                previous_active=None,
                active=None,
                switched_to=None,
                marked_bad=False,
                reason=reason,
                cooldown_minutes=cooldown_minutes,
            )

        meta = state["accounts"].get(previous)
        if meta is None:
            state["active"] = None
            save_state(paths, state)
            return RotationResult(
                previous_active=previous,
                active=None,
                switched_to=None,
                marked_bad=False,
                reason=reason,
                cooldown_minutes=cooldown_minutes,
            )

        meta["last_error"] = reason
        meta["fail_count"] = int(meta.get("fail_count", 0)) + 1
        if cooldown_minutes > 0:
            meta["cooldown_until"] = (utc_now() + timedelta(minutes=cooldown_minutes)).isoformat()
        else:
            meta["cooldown_until"] = None
        state["active"] = None
        state = sync_state_from_disk(paths, state)

        switched_to = None
        if force_switch or switch_mode == "auto":
            switched_to = _best_switch_candidate(paths, state, exclude=previous)
            if switched_to:
                _copy_active_runtime(paths, switched_to)
                state["active"] = switched_to
                state = sync_state_from_disk(paths, state)
                _sync_runtime_to_live_dir(paths, state)

        save_state(paths, state)
        return RotationResult(
            previous_active=previous,
            active=state.get("active"),
            switched_to=switched_to,
            marked_bad=True,
            reason=reason,
            cooldown_minutes=cooldown_minutes,
        )


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
    with manager_lock(paths):
        state = sync_state_from_disk(paths, load_state(paths))
        live_dir = get_live_dir(state) or default_live_dir()
        state["live_dir"] = str(live_dir.resolve())
        save_state(paths, state)

    runtime_home = live_dir.parent
    runtime_home.mkdir(parents=True, exist_ok=True)
    _remove_managed_profile_files(live_dir)

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

    if not live_dir.is_dir() or not profile_has_login_artifacts(live_dir):
        raise ValueError("agy login did not produce a usable auth profile.")

    identity = resolve_login_profile_identity(live_dir, agy_binary=resolved_binary, live_dir=live_dir)
    detected_name = identity.get("account_name")
    storage_name = normalize_account_storage_name(detected_name or name)
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
    save_state(paths, state)
    lines = [
        f"root: {paths.root}",
        f"runtime: {paths.runtime_dir}",
        f"lock: {paths.lock_file}",
        f"live_dir: {state.get('live_dir') or '-'}",
        f"active: {state.get('active') or '-'}",
        f"switch_mode: {get_switch_mode(state)}",
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
        suffix = f" [{' ; '.join(extra)}]" if extra else ""
        lines.append(f"  - {name}: {meta.get('status', 'standby')} ({flag}){suffix}")
    if not state["accounts"]:
        lines.append("  - none")
    return "\n".join(lines)

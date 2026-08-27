"""Watch Antigravity CLI logs and fail over when quota is exhausted.

`agy` never calls the manager. Cached Cloud Code usage is advisory. This module
tails the live CLI log files and treats a real `RESOURCE_EXHAUSTED` / Individual
quota banner as the failover signal.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_WATCH_POLL_SECONDS = 1.0
DEFAULT_WATCH_COOLDOWN_MINUTES = 60
LOG_WATCH_STATE_NAME = "log-watch.json"
MAX_EVENT_LINE_CHARS = 300

INDIVIDUAL_QUOTA_RE = re.compile(r"Individual quota reached", re.IGNORECASE)
RESOURCE_EXHAUSTED_RE = re.compile(r"RESOURCE_EXHAUSTED\s*\(\s*code\s*429\s*\)", re.IGNORECASE)
WEEKLY_QUOTA_RE = re.compile(r"weekly quota reached", re.IGNORECASE)
RESET_HINT_RE = re.compile(r"Resets in\s+(?P<reset>~?[^.)]+)", re.IGNORECASE)


@dataclass(frozen=True)
class QuotaLogEvent:
    kind: str
    path: str
    reset_hint: str | None
    line: str


@dataclass
class WatchPollResult:
    events: list[QuotaLogEvent]
    rotated: bool
    rotation: Any | None
    switch_mode: str
    restart_required: bool
    message: str
    files_tracked: int


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_watch_state_path(root: Path) -> Path:
    return root / LOG_WATCH_STATE_NAME


def _default_log_watch_state() -> dict:
    return {
        "cursors": {},
        "last_event_at": None,
        "last_kind": None,
        "last_path": None,
        "restart_required": False,
        "restart_armed_at": None,
        "restart_armed_account": None,
        "restart_source_logs": [],
        "initialized": False,
        "updated_at": None,
    }


def _normalize_log_watch_state(raw: object) -> dict:
    data = _default_log_watch_state()
    if not isinstance(raw, dict):
        return data
    cursors = raw.get("cursors")
    if isinstance(cursors, dict):
        clean: dict[str, dict[str, int]] = {}
        for key, value in cursors.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            try:
                offset = int(value.get("offset", 0) or 0)
            except (TypeError, ValueError):
                continue
            clean[key] = {"offset": max(0, offset)}
        data["cursors"] = clean
    for key in ("last_event_at", "last_kind", "last_path", "updated_at", "restart_armed_at", "restart_armed_account"):
        value = raw.get(key)
        data[key] = value if isinstance(value, str) or value is None else str(value)
    data["restart_required"] = bool(raw.get("restart_required"))
    source_logs = raw.get("restart_source_logs")
    if isinstance(source_logs, list):
        data["restart_source_logs"] = [item for item in source_logs if isinstance(item, str)]
    if "initialized" in raw:
        data["initialized"] = bool(raw.get("initialized"))
    else:
        # Legacy files without the marker already completed a poll if they have cursors.
        data["initialized"] = bool(data["cursors"])
    return data


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_log_watch_state(root: Path) -> dict:
    path = log_watch_state_path(root)
    if not path.is_file():
        return _default_log_watch_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_log_watch_state()
    return _normalize_log_watch_state(raw)


def save_log_watch_state(root: Path, state: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = _normalize_log_watch_state(state)
    payload["updated_at"] = _utc_now_iso()
    _atomic_write_json(log_watch_state_path(root), payload)


def _arm_restart(state: dict, *, account: str | None, source_logs: list[str]) -> None:
    state["restart_required"] = True
    state["restart_armed_at"] = _utc_now_iso()
    state["restart_armed_account"] = account
    state["restart_source_logs"] = list(source_logs)


def _disarm_restart(state: dict, *, keep_source_logs: bool = False) -> None:
    state["restart_required"] = False
    state["restart_armed_at"] = None
    state["restart_armed_account"] = None
    if not keep_source_logs:
        state["restart_source_logs"] = []


def get_log_watch_snapshot(paths: Any) -> dict:
    state = load_log_watch_state(paths.root)
    return {
        "files_tracked": len(state.get("cursors") or {}),
        "last_event_at": state.get("last_event_at"),
        "last_kind": state.get("last_kind"),
        "last_path": state.get("last_path"),
        "restart_required": bool(state.get("restart_required")),
        "restart_armed_at": state.get("restart_armed_at"),
        "restart_armed_account": state.get("restart_armed_account"),
        "initialized": bool(state.get("initialized")),
        "updated_at": state.get("updated_at"),
    }


def clear_restart_required(paths: Any) -> dict:
    """Acknowledge that agy was restarted after a log-watch rotation."""
    from agy_cli_manager.manager import manager_lock

    with manager_lock(paths):
        state = load_log_watch_state(paths.root)
        _disarm_restart(state)
        save_log_watch_state(paths.root, state)
        return load_log_watch_state(paths.root)


def resolve_antigravity_cli_dir(live_dir: Path) -> Path | None:
    direct = live_dir / "antigravity-cli"
    nested = live_dir / ".gemini" / "antigravity-cli"
    if direct.is_dir():
        return direct
    if nested.is_dir():
        return nested
    return None


def iter_live_agy_log_files(live_dir: Path | None) -> list[Path]:
    if live_dir is None:
        return []
    base_dir = resolve_antigravity_cli_dir(live_dir)
    if base_dir is None:
        return []
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


def parse_quota_log_line(line: str) -> QuotaLogEvent | None:
    text = line.strip()
    if not text:
        return None
    reset_match = RESET_HINT_RE.search(text)
    reset_hint = reset_match.group("reset").strip() if reset_match else None
    if INDIVIDUAL_QUOTA_RE.search(text) or (
        RESOURCE_EXHAUSTED_RE.search(text) and "quota reached" in text.lower()
    ):
        kind = "individual_quota"
    elif WEEKLY_QUOTA_RE.search(text):
        kind = "weekly_quota"
    else:
        return None
    clipped = text if len(text) <= MAX_EVENT_LINE_CHARS else text[: MAX_EVENT_LINE_CHARS - 3] + "..."
    return QuotaLogEvent(kind=kind, path="", reset_hint=reset_hint, line=clipped)


def read_new_complete_lines(path: Path, offset: int) -> tuple[int, list[str]]:
    try:
        size = path.stat().st_size
    except OSError:
        return offset, []
    if size < offset:
        offset = 0
    if size == offset:
        return offset, []
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return offset, []
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        return offset, []
    chunk = data[: last_nl + 1]
    new_offset = offset + len(chunk)
    text = chunk.decode("utf-8", errors="replace")
    return new_offset, text.splitlines()


def initial_log_offset(
    path: Path,
    *,
    from_start: bool,
    started_at: float | None = None,
    known: bool = False,
    initialized: bool = False,
) -> int:
    # started_at is kept for call-site compatibility. Existing files are
    # initialized at EOF; mtime is not used because writes refresh it.
    del started_at
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if from_start:
        return 0
    if known:
        return 0
    if initialized:
        return 0
    return size


def consume_log_events(
    live_dir: Path | None,
    cursors: dict[str, dict[str, int]],
    *,
    from_start: bool = False,
    started_at: float | None = None,
    initialized: bool = False,
) -> tuple[dict[str, dict[str, int]], list[QuotaLogEvent]]:
    events: list[QuotaLogEvent] = []
    next_cursors = dict(cursors)
    for path in iter_live_agy_log_files(live_dir):
        key = str(path)
        known = key in next_cursors
        if known and not from_start:
            offset = int(next_cursors[key].get("offset", 0) or 0)
        else:
            offset = initial_log_offset(
                path,
                from_start=from_start,
                started_at=started_at,
                known=known,
                initialized=initialized,
            )
        new_offset, lines = read_new_complete_lines(path, offset)
        next_cursors[key] = {"offset": new_offset}
        for line in lines:
            parsed = parse_quota_log_line(line)
            if parsed is None:
                continue
            events.append(
                QuotaLogEvent(
                    kind=parsed.kind,
                    path=key,
                    reset_hint=parsed.reset_hint,
                    line=parsed.line,
                )
            )
    return next_cursors, events


def _rotation_payload(result: Any | None) -> dict | None:
    if result is None:
        return None
    return {
        "previous_active": result.previous_active,
        "active": result.active,
        "switched_to": result.switched_to,
        "marked_bad": result.marked_bad,
        "reason": result.reason,
        "cooldown_minutes": result.cooldown_minutes,
        "outcome": result.outcome,
    }


def _record_last_event(watch_state: dict, events: list[QuotaLogEvent]) -> None:
    if not events:
        return
    watch_state["last_event_at"] = _utc_now_iso()
    watch_state["last_kind"] = events[-1].kind
    watch_state["last_path"] = events[-1].path


def poll_quota_logs(
    paths: Any,
    *,
    from_start: bool = False,
    started_at: float | None = None,
    rotate: bool = True,
    force_switch: bool = False,
    cooldown_minutes: int = DEFAULT_WATCH_COOLDOWN_MINUTES,
    on_rotate: str | None = None,
) -> WatchPollResult:
    from agy_cli_manager.manager import (
        get_live_dir,
        get_switch_mode,
        load_state,
        manager_lock,
        rotate_after_failure_locked,
    )

    on_rotate_cmd = None
    with manager_lock(paths):
        state = load_state(paths)
        live_dir = get_live_dir(state)
        switch_mode = get_switch_mode(state)
        watch_state = load_log_watch_state(paths.root)
        cursors = dict(watch_state.get("cursors") or {})
        initialized = bool(watch_state.get("initialized"))
        next_cursors, events = consume_log_events(
            live_dir,
            cursors,
            from_start=from_start,
            started_at=started_at,
            initialized=initialized,
        )
        watch_state["cursors"] = next_cursors
        watch_state["initialized"] = True
        rotation = None
        rotated = False
        message = "no quota errors"

        ignored_logs = set(watch_state.get("restart_source_logs") or [])
        new_session_logs = {key for key in next_cursors if key not in ignored_logs}
        if watch_state.get("restart_required") and new_session_logs:
            _disarm_restart(watch_state, keep_source_logs=True)

        events_for_rotate = [event for event in events if event.path not in ignored_logs]

        if events:
            _record_last_event(watch_state, events)

        if not events_for_rotate:
            if events and watch_state.get("restart_required"):
                message = "quota error observed; waiting for agy restart"
            elif events and ignored_logs:
                message = "agy restart detected; old quota errors ignored"
        elif events_for_rotate:
            should_rotate = rotate and (force_switch or switch_mode == "auto")
            if should_rotate:
                rotation = rotate_after_failure_locked(
                    paths,
                    reason="quota",
                    cooldown_minutes=cooldown_minutes,
                    force_switch=force_switch,
                    trigger="log-watch",
                )
                rotated = rotation.outcome == "switched"
                if rotated:
                    _arm_restart(
                        watch_state,
                        account=rotation.switched_to,
                        source_logs=sorted(next_cursors),
                    )
                    message = (
                        f"rotated {rotation.previous_active} -> {rotation.switched_to}; "
                        "restart agy to pick up the new token"
                    )
                    if on_rotate:
                        on_rotate_cmd = on_rotate
                elif rotation.outcome == "already_switched":
                    message = f"already switched to {rotation.active or '-'}"
                elif switch_mode == "manual" and not force_switch:
                    message = "quota error observed; switch-mode is manual"
                else:
                    message = f"quota error observed; outcome={rotation.outcome}"
            else:
                message = (
                    f"quota error observed ({events_for_rotate[-1].kind}); "
                    "rotation skipped"
                )
                if switch_mode == "manual" and not force_switch:
                    message = "quota error observed; switch-mode is manual"

        save_log_watch_state(paths.root, watch_state)
        result = WatchPollResult(
            events=events,
            rotated=rotated,
            rotation=rotation,
            switch_mode=switch_mode,
            restart_required=bool(watch_state.get("restart_required")),
            message=message,
            files_tracked=len(next_cursors),
        )
    if on_rotate_cmd:
        subprocess.run(on_rotate_cmd, shell=True, check=False)
    return result


def watch_poll_payload(result: WatchPollResult) -> dict:
    return {
        "events": [asdict(event) for event in result.events],
        "rotated": result.rotated,
        "rotation": _rotation_payload(result.rotation),
        "switch_mode": result.switch_mode,
        "restart_required": result.restart_required,
        "message": result.message,
        "files_tracked": result.files_tracked,
    }


def format_watch_poll(result: WatchPollResult) -> str:
    if not result.events and not result.rotated:
        return f"watch: {result.message}"
    parts = [
        f"watch: {result.message}",
        f"events={len(result.events)}",
        f"kind={result.events[-1].kind if result.events else '-'}",
        f"reset={result.events[-1].reset_hint if result.events and result.events[-1].reset_hint else '-'}",
        f"mode={result.switch_mode}",
    ]
    return " ".join(parts)


def watch_quota_logs(
    paths: Any,
    *,
    follow: bool = True,
    once: bool = False,
    from_start: bool = False,
    poll_seconds: float = DEFAULT_WATCH_POLL_SECONDS,
    rotate: bool = True,
    force_switch: bool = False,
    cooldown_minutes: int = DEFAULT_WATCH_COOLDOWN_MINUTES,
    on_rotate: str | None = None,
    as_json: bool = False,
    printer=print,
) -> int:
    started_at = time.time()
    interval = poll_seconds if poll_seconds > 0 else DEFAULT_WATCH_POLL_SECONDS
    first = True
    while True:
        result = poll_quota_logs(
            paths,
            from_start=from_start and first,
            started_at=started_at,
            rotate=rotate,
            force_switch=force_switch,
            cooldown_minutes=cooldown_minutes,
            on_rotate=on_rotate,
        )
        first = False
        if result.events or result.rotated or once or as_json:
            if as_json:
                printer(json.dumps(watch_poll_payload(result), indent=2, sort_keys=True))
            elif result.events or result.rotated or once:
                printer(format_watch_poll(result))
        if once or not follow:
            return 0
        time.sleep(interval)

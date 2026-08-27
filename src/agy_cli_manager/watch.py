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
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

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
    for key in ("last_event_at", "last_kind", "last_path", "updated_at"):
        value = raw.get(key)
        data[key] = value if isinstance(value, str) or value is None else str(value)
    data["restart_required"] = bool(raw.get("restart_required"))
    return data


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
    path = log_watch_state_path(root)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def get_log_watch_snapshot(paths: Any) -> dict:
    state = load_log_watch_state(paths.root)
    return {
        "files_tracked": len(state.get("cursors") or {}),
        "last_event_at": state.get("last_event_at"),
        "last_kind": state.get("last_kind"),
        "last_path": state.get("last_path"),
        "restart_required": bool(state.get("restart_required")),
        "updated_at": state.get("updated_at"),
    }


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
    started_at: float | None,
    known: bool,
) -> int:
    try:
        stat_result = path.stat()
    except OSError:
        return 0
    if from_start or not known:
        if from_start:
            return 0
        if started_at is not None and stat_result.st_mtime >= (started_at - 1.0):
            return 0
        return stat_result.st_size
    return 0


def consume_log_events(
    live_dir: Path | None,
    cursors: dict[str, dict[str, int]],
    *,
    from_start: bool = False,
    started_at: float | None = None,
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
        rotate_after_failure,
    )

    state = load_state(paths)
    live_dir = get_live_dir(state)
    switch_mode = get_switch_mode(state)
    watch_state = load_log_watch_state(paths.root)
    cursors = dict(watch_state.get("cursors") or {})
    next_cursors, events = consume_log_events(
        live_dir,
        cursors,
        from_start=from_start,
        started_at=started_at,
    )
    watch_state["cursors"] = next_cursors
    rotation = None
    rotated = False
    message = "no quota errors"

    if events:
        watch_state["last_event_at"] = _utc_now_iso()
        watch_state["last_kind"] = events[-1].kind
        watch_state["last_path"] = events[-1].path
        should_rotate = rotate and (force_switch or switch_mode == "auto")
        if should_rotate:
            rotation = rotate_after_failure(
                paths,
                reason="quota",
                cooldown_minutes=cooldown_minutes,
                force_switch=force_switch,
                trigger="log-watch",
            )
            rotated = rotation.outcome == "switched"
            if rotated:
                watch_state["restart_required"] = True
                message = (
                    f"rotated {rotation.previous_active} -> {rotation.switched_to}; "
                    "restart agy to pick up the new token"
                )
                if on_rotate:
                    subprocess.run(on_rotate, shell=True, check=False)
            elif rotation.outcome == "already_switched":
                message = f"already switched to {rotation.active or '-'}"
            elif switch_mode == "manual" and not force_switch:
                message = "quota error observed; switch-mode is manual"
            else:
                message = f"quota error observed; outcome={rotation.outcome}"
        else:
            message = (
                f"quota error observed ({events[-1].kind}); "
                "rotation skipped"
            )
            if switch_mode == "manual" and not force_switch:
                message = "quota error observed; switch-mode is manual"

    save_log_watch_state(paths.root, watch_state)
    return WatchPollResult(
        events=events,
        rotated=rotated,
        rotation=rotation,
        switch_mode=switch_mode,
        restart_required=bool(watch_state.get("restart_required")),
        message=message,
        files_tracked=len(next_cursors),
    )


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


def resume_agy_args(argv: Sequence[str]) -> list[str]:
    args = [str(arg) for arg in argv]
    if args[:1] == ["--"]:
        args = args[1:]
    for arg in args:
        if arg in {"-c", "--continue"}:
            return args
        if arg == "--conversation" or arg.startswith("--conversation="):
            return args
    return ["--continue", *args]


def clear_restart_required(root: Path) -> None:
    state = load_log_watch_state(root)
    if not state.get("restart_required"):
        return
    state["restart_required"] = False
    save_log_watch_state(root, state)


def _stop_agy_process(proc: subprocess.Popen[Any], *, timeout: float = 8.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_agy_with_quota_failover(
    paths: Any,
    agy_args: Sequence[str] | None = None,
    *,
    agy_binary: str | None = None,
    poll_seconds: float = DEFAULT_WATCH_POLL_SECONDS,
    cooldown_minutes: int = DEFAULT_WATCH_COOLDOWN_MINUTES,
    force_switch: bool = False,
    printer=print,
) -> int:
    from agy_cli_manager.manager import (
        _agy_subprocess_env,
        apply_active,
        get_live_dir,
        load_state,
        resolve_agy_binary,
    )

    interval = poll_seconds if poll_seconds > 0 else DEFAULT_WATCH_POLL_SECONDS
    original_args = [str(arg) for arg in (agy_args or ())]
    if original_args[:1] == ["--"]:
        original_args = original_args[1:]
    binary = resolve_agy_binary(agy_binary)
    session = 0
    proc: subprocess.Popen[Any] | None = None

    printer("agy-cli-manager run: quota full -> switch account -> continue same chat")
    try:
        while True:
            active = apply_active(paths)
            state = load_state(paths)
            live_dir = get_live_dir(state)
            if live_dir is None:
                raise ValueError("No live Antigravity directory is set.")
            argv = list(original_args) if session == 0 else resume_agy_args(original_args)
            env = _agy_subprocess_env(live_dir.parent)
            if session > 0:
                clear_restart_required(paths.root)
                printer(f"restarting agy as {active} with {' '.join(argv) or '--continue'}")
            else:
                printer(f"starting agy as {active}")
            proc = subprocess.Popen(
                [binary, *argv],
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
                cwd=os.getcwd(),
                env=env,
            )
            rotated = False
            while proc.poll() is None:
                result = poll_quota_logs(
                    paths,
                    rotate=True,
                    force_switch=force_switch,
                    cooldown_minutes=cooldown_minutes,
                )
                if result.rotated:
                    rotated = True
                    printer(result.message)
                    printer("quota full; switching account and continuing")
                    _stop_agy_process(proc)
                    break
                try:
                    proc.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
            if rotated:
                session += 1
                continue
            code = proc.returncode if proc.returncode is not None else 0
            return int(code)
    except KeyboardInterrupt:
        if proc is not None:
            _stop_agy_process(proc)
        return 130
    finally:
        if proc is not None and proc.poll() is None:
            _stop_agy_process(proc)

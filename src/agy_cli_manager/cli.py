from __future__ import annotations

import argparse
import curses
import json
import textwrap
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, SimpleQueue

from agy_cli_manager.manager import (
    add_account,
    apply_active,
    build_paths,
    clear_bad,
    default_root,
    ensure_layout,
    format_status,
    get_account_identity,
    get_live_dir,
    get_status_snapshot,
    import_current,
    login_account,
    load_state,
    mark_bad,
    probe_profile_identity_via_usage,
    refresh_account_usage,
    refresh_account_identity,
    rotate_after_failure,
    set_live_dir,
    set_enabled,
    switch_account,
    switch_next,
    update_account_runtime_metadata,
)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agy-cli-manager")
    parser.add_argument("--root", type=Path, default=default_root(), help="Manager root directory")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Create initial manager layout")
    sub.add_parser("dashboard", help="Open the full-screen dashboard")
    sub.add_parser("menu", help="Open the interactive menu")
    status = sub.add_parser("status", help="Show current manager status")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    current = sub.add_parser("current", help="Show the current active account")
    current.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    list_cmd = sub.add_parser("list", help="List saved accounts")
    list_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    sub.add_parser("apply-active", help="Re-apply the current active account to runtime and live_dir")
    refresh_usage = sub.add_parser("refresh-usage", help="Fetch real Cloud Code quota and persist cached usage metadata")
    refresh_usage.add_argument("name", nargs="?")
    refresh_usage.add_argument("--agy-binary")
    refresh_usage.add_argument("--warmup-timeout-seconds", type=int, default=45)
    refresh_usage.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    whoami = sub.add_parser("whoami", help="Show the detected account identity for the active or named profile")
    whoami.add_argument("name", nargs="?")
    whoami.add_argument("--refresh", action="store_true", help="Refresh cached identity from profile files")
    whoami.add_argument("--probe-usage", action="store_true", help="Run `agy -p /usage` against the selected profile")
    whoami.add_argument("--agy-binary")
    whoami.add_argument("--timeout-seconds", type=int, default=30)

    add = sub.add_parser("add", help="Add an account profile from a source directory")
    add.add_argument("name")
    add.add_argument("source_dir", type=Path)

    import_cmd = sub.add_parser("import-current", help="Import the current live_dir or a provided source dir as an account")
    import_cmd.add_argument("name")
    import_cmd.add_argument("source_dir", type=Path, nargs="?")

    login = sub.add_parser("login", help="Run isolated agy login and save the resulting profile")
    login.add_argument("name", nargs="?")
    login.add_argument("--agy-binary")
    login.add_argument("--timeout-seconds", type=int, default=600)

    switch = sub.add_parser("switch", help="Switch to a named account")
    switch.add_argument("name")
    activate = sub.add_parser("activate", help="Alias for switch")
    activate.add_argument("name")

    sub.add_parser("switch-next", help="Switch to the next enabled standby account")
    rotate_cmd = sub.add_parser("rotate", help="Alias for switch-next")
    rotate_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    disable = sub.add_parser("disable", help="Disable an account")
    disable.add_argument("name")

    enable = sub.add_parser("enable", help="Enable an account")
    enable.add_argument("name")

    mark = sub.add_parser("mark-bad", help="Mark an account bad and optionally put it in cooldown")
    mark.add_argument("name")
    mark.add_argument("--reason", default="manual")
    mark.add_argument("--cooldown-minutes", type=int, default=60)

    clear = sub.add_parser("clear-bad", help="Clear cooldown/error state for an account")
    clear.add_argument("name")

    live = sub.add_parser("set-live-dir", help="Set or clear a real live CLI home directory")
    live.add_argument("path", nargs="?")

    rotate = sub.add_parser("rotate-after-failure", help="Mark the active account bad and switch to the next standby account")
    rotate.add_argument("--reason", default="manual")
    rotate.add_argument("--cooldown-minutes", type=int, default=60)
    rotate.add_argument("--live-dir")
    rotate.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    update_meta = sub.add_parser("update-meta", help="Update cached runtime metadata for an account")
    update_meta.add_argument("name")
    update_meta.add_argument("--usage-status")
    update_meta.add_argument("--usage-value")
    update_meta.add_argument("--reset-at")
    update_meta.add_argument("--short-usage-status")
    update_meta.add_argument("--short-usage-value")
    update_meta.add_argument("--short-reset-at")
    update_meta.add_argument("--weekly-usage-status")
    update_meta.add_argument("--weekly-usage-value")
    update_meta.add_argument("--weekly-reset-at")
    update_meta.add_argument("--health-status")
    update_meta.add_argument("--last-live-check-at")
    update_meta.add_argument("--last-live-check-error")
    update_meta.add_argument("--next-live-check-at")
    update_meta.add_argument("--refresh-policy-seconds", type=int)
    update_meta.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def prompt_nonempty(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("Value cannot be empty.")


def prompt_optional_path(label: str) -> Path | None:
    value = input(f"{label} (leave empty to skip): ").strip()
    if not value:
        return None
    return Path(value).expanduser()


def prompt_optional_text(label: str) -> str | None:
    value = input(f"{label}: ").strip()
    return value or None


def run_login_with_prompt(
    paths,
    name: str,
    agy_binary: str | None,
    timeout_seconds: int,
) -> str | None:
    try:
        return login_account(paths, name, agy_binary, timeout_seconds)
    except ValueError as exc:
        message = str(exc)
        if "agy binary not found" not in message or not sys.stdin.isatty():
            raise
        print(f"error: {message}")
        retry_binary = prompt_optional_text("agy binary path")
        if not retry_binary:
            raise ValueError("agy binary path is required.")
        return login_account(paths, name, retry_binary, timeout_seconds)


def run_menu(paths, parser: argparse.ArgumentParser) -> int:
    ensure_layout(paths)
    while True:
        print("\nagy-cli-manager")
        print("1. Status")
        print("2. Login account")
        print("3. Import current/live profile")
        print("4. Switch account")
        print("5. Switch next")
        print("6. Set live dir")
        print("7. Disable account")
        print("8. Enable account")
        print("9. Mark account bad")
        print("10. Clear account bad state")
        print("11. Show account identity")
        print("0. Exit")

        choice = input("Select: ").strip()
        try:
            if choice == "1":
                print(format_status(paths))
            elif choice == "2":
                name = prompt_nonempty("Account name")
                agy_binary = input("agy binary [auto]: ").strip() or None
                timeout_raw = input("timeout seconds [600]: ").strip() or "600"
                stored_name = run_login_with_prompt(paths, name, agy_binary, int(timeout_raw))
                print(f"{'logged-in' if stored_name else 'cancelled'}: {stored_name or name}")
            elif choice == "3":
                name = prompt_nonempty("Account name")
                source_dir = prompt_optional_path("Source dir")
                import_current(paths, name, source_dir)
                print(f"imported-current: {name}")
            elif choice == "4":
                name = prompt_nonempty("Account name")
                previous = switch_account(paths, name)
                print(f"switched: {previous + ' -> ' if previous else ''}{name}")
            elif choice == "5":
                print(f"switched-next: {switch_next(paths)}")
            elif choice == "6":
                live_dir = prompt_optional_path("Live dir")
                set_live_dir(paths, live_dir)
                print(f"live-dir: {live_dir if live_dir else 'cleared'}")
            elif choice == "7":
                name = prompt_nonempty("Account name")
                set_enabled(paths, name, False)
                print(f"disabled: {name}")
            elif choice == "8":
                name = prompt_nonempty("Account name")
                set_enabled(paths, name, True)
                print(f"enabled: {name}")
            elif choice == "9":
                name = prompt_nonempty("Account name")
                reason = input("Reason [manual]: ").strip() or "manual"
                cooldown_raw = input("Cooldown minutes [60]: ").strip() or "60"
                mark_bad(paths, name, reason, int(cooldown_raw))
                print(f"marked-bad: {name}")
            elif choice == "10":
                name = prompt_nonempty("Account name")
                clear_bad(paths, name)
                print(f"cleared-bad: {name}")
            elif choice == "11":
                name_raw = input("Account name (leave empty for active): ").strip()
                name = name_raw or None
                resolved_name, identity = (
                    (name, refresh_account_identity(paths, name))
                    if name
                    else get_account_identity(paths)
                )
                print(f"account: {resolved_name}")
                print(f"account_name: {identity.get('account_name') or '-'}")
                print(f"source: {identity.get('source') or '-'}")
            elif choice == "0":
                return 0
            else:
                print("Unknown selection.")
        except ValueError as e:
            print(f"error: {e}")
        except KeyboardInterrupt:
            print("\nCancelled.")
    return 0


def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    clipped = text[: max(0, width - x - 1)]
    if not clipped:
        return
    try:
        stdscr.addstr(y, x, clipped, attr)
    except curses.error:
        pass


def _draw_hline(stdscr, y: int, ch: str = "-") -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height:
        return
    _safe_addstr(stdscr, y, 0, ch * max(0, width - 1))


def _draw_wrapped_lines(stdscr, start_y: int, text: str, attr: int = 0) -> int:
    height, width = stdscr.getmaxyx()
    wrap_width = max(20, width - 1)
    lines = textwrap.wrap(text, width=wrap_width, break_long_words=False, break_on_hyphens=False) or [""]
    used = 0
    for idx, line in enumerate(lines):
        y = start_y + idx
        if y >= height:
            break
        _safe_addstr(stdscr, y, 0, line, attr)
        used += 1
    return used


COLOR_HEADER = 1
COLOR_ACTIONS = 2
COLOR_SECTION = 3
COLOR_GOOD = 4
COLOR_WARN = 5
COLOR_BAD = 6
COLOR_ACTIVE = 7
COLOR_MUTED = 8
COLOR_INFO = 9
COLOR_SELECTED = 10
COLOR_LABEL = 11


def _init_dashboard_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    if getattr(curses, "COLORS", 0) >= 16:
        palette = {
            COLOR_HEADER: 15,
            COLOR_ACTIONS: 14,
            COLOR_SECTION: 13,
            COLOR_GOOD: 10,
            COLOR_WARN: 11,
            COLOR_BAD: 9,
            COLOR_ACTIVE: 14,
            COLOR_MUTED: 15,
            COLOR_INFO: 14,
            COLOR_SELECTED: 15,
            COLOR_LABEL: 13,
        }
        for pair_id, color_id in palette.items():
            curses.init_pair(pair_id, color_id, -1)
        return
    curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_ACTIONS, curses.COLOR_BLUE, -1)
    curses.init_pair(COLOR_SECTION, curses.COLOR_MAGENTA, -1)
    curses.init_pair(COLOR_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_BAD, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_ACTIVE, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_MUTED, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_INFO, curses.COLOR_BLUE, -1)
    curses.init_pair(COLOR_SELECTED, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_LABEL, curses.COLOR_MAGENTA, -1)


def _color_attr(pair_id: int, extra: int = 0) -> int:
    if curses.has_colors():
        return curses.color_pair(pair_id) | extra
    return extra


def _severity_from_remaining_percent(value: float | None) -> str:
    if value is None:
        return "muted"
    if value <= 10:
        return "bad"
    if value <= 35:
        return "warn"
    return "good"


def _severity_attr(severity: str, selected: bool = False, bold: bool = False) -> int:
    extra = 0
    if bold:
        extra |= curses.A_BOLD
    if selected:
        extra |= curses.A_BOLD
    if severity == "good":
        return _color_attr(COLOR_GOOD, extra)
    if severity == "warn":
        return _color_attr(COLOR_WARN, extra)
    if severity == "bad":
        return _color_attr(COLOR_BAD, extra)
    if severity == "active":
        return _color_attr(COLOR_ACTIVE, extra)
    if severity == "info":
        return _color_attr(COLOR_INFO, extra)
    if severity == "selected":
        return _color_attr(COLOR_SELECTED, extra | curses.A_BOLD)
    if severity == "label":
        return _color_attr(COLOR_LABEL, extra | curses.A_BOLD)
    return _color_attr(COLOR_MUTED, extra)


def _selected_marker_attr(selected: bool) -> int:
    return _severity_attr("selected" if selected else "muted", False, bold=selected)


def _selected_name_attr(state: str, selected: bool) -> int:
    normalized = (state or "").lower()
    if normalized == "active":
        return _severity_attr("active", selected, bold=True)
    if selected:
        return _severity_attr("selected", False, bold=True)
    return _severity_attr("muted", False)


def _state_attr(state: str, selected: bool = False) -> int:
    normalized = (state or "").lower()
    if normalized == "active":
        return _severity_attr("active", selected, bold=True)
    if normalized in {"healthy", "ok"}:
        return _severity_attr("good", selected)
    if normalized in {"ready"}:
        return _severity_attr("good", selected, bold=True)
    if normalized in {"cooldown", "disabled", "auth_expired"}:
        return _severity_attr("warn", selected)
    if normalized in {"bad", "error", "failed", "stale", "refresh_failed", "auth_missing"}:
        return _severity_attr("bad", selected)
    return _severity_attr("muted", selected)


def _usage_window_values(meta: dict) -> tuple[float | None, float | None]:
    windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
    short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
    weekly = windows.get("weekly") if isinstance(windows.get("weekly"), dict) else {}
    short_value = short.get("value") if isinstance(short.get("value"), (int, float)) else None
    weekly_value = weekly.get("value") if isinstance(weekly.get("value"), (int, float)) else None
    return short_value, weekly_value


def _usage_attr(meta: dict, selected: bool = False) -> int:
    values = [value for value in _usage_window_values(meta) if value is not None]
    remaining = min(values) if values else None
    return _severity_attr(_severity_from_remaining_percent(remaining), selected, bold=True)


def _reset_attr(meta: dict, now: datetime, selected: bool = False) -> int:
    windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
    reset_times = []
    for key in ("short", "weekly"):
        window = windows.get(key) if isinstance(windows.get(key), dict) else {}
        reset_at = _parse_iso_timestamp(window.get("reset_at"))
        if reset_at is not None:
            reset_times.append(reset_at)
    if not reset_times:
        return _severity_attr("muted", selected)
    soonest = min(reset_times)
    minutes = int((soonest - now).total_seconds() // 60)
    if minutes <= 0:
        return _severity_attr("bad", selected, bold=True)
    if minutes < 60:
        return _severity_attr("warn", selected)
    return _severity_attr("good", selected)


def _next_refresh_attr(meta: dict, now: datetime, selected: bool = False) -> int:
    next_check = _parse_iso_timestamp(meta.get("next_live_check_at"))
    if not next_check:
        return _severity_attr("muted", selected)
    if next_check <= now:
        return _severity_attr("bad", selected, bold=True)
    if (next_check - now).total_seconds() < 300:
        return _severity_attr("warn", selected)
    return _severity_attr("good", selected)


def _message_attr(message: str) -> int:
    lowered = message.lower()
    if "failed" in lowered or lowered.startswith("error:"):
        return _severity_attr("bad", bold=True)
    if "due" in lowered or "refresh" in lowered or "rotated" in lowered:
        return _severity_attr("warn", bold=True)
    return _severity_attr("good", bold=True)


def _draw_segments(stdscr, y: int, segments: list[tuple[str, int]]) -> None:
    x = 0
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height:
        return
    for text, attr in segments:
        if x >= width - 1:
            break
        clipped = text[: max(0, width - x - 1)]
        if clipped:
            _safe_addstr(stdscr, y, x, clipped, attr)
        x += len(clipped)


def _draw_detail_line(stdscr, y: int, label: str, value: str, value_attr: int = 0) -> None:
    _draw_segments(
        stdscr,
        y,
        [
            (f"{label}: ", _severity_attr("label")),
            (value, value_attr),
        ],
    )


def _draw_detail_line_at(stdscr, y: int, x: int, label: str, value: str, value_attr: int = 0) -> None:
    segments = [
        (f"{label}: ", _severity_attr("label")),
        (value, value_attr),
    ]
    offset = x
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    for text, attr in segments:
        if offset >= width - 1:
            break
        clipped = text[: max(0, width - offset - 1)]
        if clipped:
            _safe_addstr(stdscr, y, offset, clipped, attr)
        offset += len(clipped)


def _draw_legend(stdscr, y: int) -> int:
    height, _width = stdscr.getmaxyx()
    if y >= height:
        return 0
    _draw_segments(
        stdscr,
        y,
        [
            ("Legend ", _severity_attr("label")),
            ("> selected", _severity_attr("selected", bold=True)),
            ("  ", 0),
            ("* active", _severity_attr("active", bold=True)),
            ("  ", 0),
            ("green ready", _severity_attr("good")),
            ("  ", 0),
            ("yellow expiring", _severity_attr("warn")),
            ("  ", 0),
            ("red failed", _severity_attr("bad")),
        ],
    )
    return 1


def _draw_action_bar(stdscr, y: int) -> int:
    actions = [
        ("N", "Login"),
        ("I", "Import"),
        ("Enter/A", "Activate"),
        ("R", "Rotate"),
        ("E", "Enable/Disable"),
        ("C", "ClearBad"),
        ("M", "MarkBad"),
        ("S", "Sort"),
        ("U", "Live Usage Refresh"),
        ("T", "UI Refresh"),
        ("Q", "Quit"),
    ]
    height, width = stdscr.getmaxyx()
    if y >= height:
        return 0
    x = 0
    row = 0
    prefix = "Actions: "
    _safe_addstr(stdscr, y, x, prefix, _color_attr(COLOR_ACTIONS, curses.A_BOLD))
    x += len(prefix)
    for key, label in actions:
        parts = [
            ("[", _color_attr(COLOR_MUTED)),
            (key, _severity_attr("selected", bold=True)),
            ("]", _color_attr(COLOR_MUTED)),
            (" ", _color_attr(COLOR_MUTED)),
            (label, _color_attr(COLOR_ACTIONS, curses.A_BOLD)),
            ("  ", _color_attr(COLOR_MUTED)),
        ]
        needed = sum(len(text) for text, _attr in parts)
        if x + needed >= max(0, width - 1):
            row += 1
            if y + row >= height:
                break
            x = 0
        for text, attr in parts:
            clipped = text[: max(0, width - x - 1)]
            if clipped:
                _safe_addstr(stdscr, y + row, x, clipped, attr)
            x += len(clipped)
    return row + 1


def _detail_value_attr(selected_meta: dict, label: str, now_dt: datetime) -> int:
    if label in {"Short Window", "Weekly Window"}:
        return _usage_attr(selected_meta)
    if label == "Health":
        return _state_attr(selected_meta.get("health_status", "unknown"))
    if label == "Next Refresh":
        return _next_refresh_attr(selected_meta, now_dt)
    if label in {"Last Live Error", "Last Error"}:
        value = selected_meta.get("last_live_check_error") if label == "Last Live Error" else _format_last_error(selected_meta)
        return _severity_attr("bad" if value and value != "-" else "muted")
    if label == "State":
        return _state_attr(selected_meta.get("status", "standby"))
    if label == "Failures":
        return _severity_attr("bad" if int(selected_meta.get("fail_count", 0) or 0) > 0 else "muted")
    if label == "Cooldown Until":
        return _severity_attr("warn" if selected_meta.get("cooldown_until") else "muted")
    if label == "Identity":
        return _severity_attr("info")
    return _severity_attr("muted")


def _draw_detail_block(stdscr, start_y: int, title: str, rows: list[tuple[str, str, int]], start_x: int = 0) -> int:
    _safe_addstr(stdscr, start_y, start_x, title, _color_attr(COLOR_SECTION, curses.A_BOLD))
    for idx, (label, value, value_attr) in enumerate(rows):
        _draw_detail_line_at(stdscr, start_y + 1 + idx, start_x, label, value, value_attr)
    return 1 + len(rows)


def _refresh_dashboard_snapshot(paths):
    return get_status_snapshot(paths)


def _start_usage_refresh_worker(paths, name: str, result_queue: SimpleQueue) -> threading.Thread:
    def _worker() -> None:
        try:
            result = refresh_account_usage(paths, name)
            result_queue.put(
                {
                    "account": name,
                    "ok": True,
                    "short_usage_value": result.short_usage_value,
                    "weekly_usage_value": result.weekly_usage_value,
                }
            )
        except Exception as exc:
            result_queue.put(
                {
                    "account": name,
                    "ok": False,
                    "error": str(exc),
                }
            )

    thread = threading.Thread(target=_worker, name=f"agy-usage-refresh-{name}", daemon=True)
    thread.start()
    return thread


def _format_identity(meta: dict) -> str:
    identity = meta.get("identity")
    if isinstance(identity, dict):
        return identity.get("account_name") or "-"
    return "-"


def _format_last_error(meta: dict) -> str:
    value = meta.get("last_error")
    if not value:
        return "-"
    return str(value)


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_usage(meta: dict) -> str:
    windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
    short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
    weekly = windows.get("weekly") if isinstance(windows.get("weekly"), dict) else {}
    return f"{_format_usage_value(short)}/{_format_usage_value(weekly)}"


def _format_countdown(meta: dict, now: datetime) -> str:
    windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
    short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
    weekly = windows.get("weekly") if isinstance(windows.get("weekly"), dict) else {}
    return f"{_format_reset_value(short, now)}/{_format_reset_value(weekly, now)}"


def _format_age(value: str | None, now: datetime) -> str:
    dt = _parse_iso_timestamp(value)
    if not dt:
        return "-"
    delta = max(0, int((now - dt).total_seconds()))
    minutes, seconds = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02}m ago"
    if minutes:
        return f"{minutes}m ago"
    return f"{seconds}s ago"


def _format_next_refresh(meta: dict, now: datetime) -> str:
    next_check = _parse_iso_timestamp(meta.get("next_live_check_at"))
    if not next_check:
        policy = int(meta.get("refresh_policy_seconds", 0) or 0)
        return f"{policy}s" if policy > 0 else "-"
    delta = int((next_check - now).total_seconds())
    if delta <= 0:
        return "due"
    minutes, seconds = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02}m"
    if minutes:
        return f"{minutes}m{seconds:02}s"
    return f"{seconds}s"


def _format_window_summary(meta: dict, window_name: str, now: datetime) -> str:
    windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
    window = windows.get(window_name) if isinstance(windows.get(window_name), dict) else {}
    value = window.get("value")
    status = window.get("status") or "unknown"
    reset_at = _parse_iso_timestamp(window.get("reset_at"))
    if value is None:
        usage = "-" if status == "unknown" else str(status)
    else:
        usage = str(value)
    if reset_at is None:
        countdown = "-"
    else:
        delta = int((reset_at - now).total_seconds())
        if delta <= 0:
            countdown = "due"
        else:
            minutes, seconds = divmod(delta, 60)
            hours, minutes = divmod(minutes, 60)
            countdown = f"{hours}h{minutes:02}m" if hours else (f"{minutes}m{seconds:02}s" if minutes else f"{seconds}s")
    return f"{usage} | {countdown}"


def _format_usage_value(window: dict) -> str:
    if not isinstance(window, dict):
        return "-"
    value = window.get("value")
    status = window.get("status") or "unknown"
    if isinstance(value, (int, float)):
        return f"{round(float(value))}%"
    if status == "unknown":
        return "-"
    return str(status)[:4]


def _format_reset_value(window: dict, now: datetime) -> str:
    if not isinstance(window, dict):
        return "-"
    reset_at = _parse_iso_timestamp(window.get("reset_at"))
    if not reset_at:
        return "-"
    delta = int((reset_at - now).total_seconds())
    if delta <= 0:
        return "0m"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


def _format_live_state(meta: dict, now: datetime) -> str:
    next_check = _parse_iso_timestamp(meta.get("next_live_check_at"))
    health = meta.get("health_status") or "unknown"
    if next_check and next_check <= now:
        return f"stale/{health}"[:18]
    return health[:18]


def _should_auto_refresh_usage(meta: dict, now: datetime) -> bool:
    if not isinstance(meta, dict):
        return False
    if not meta.get("enabled", True):
        return False
    status = meta.get("status") or "standby"
    if status in {"disabled", "cooldown"}:
        return False
    next_check = _parse_iso_timestamp(meta.get("next_live_check_at"))
    if next_check is not None:
        return next_check <= now
    policy = int(meta.get("refresh_policy_seconds", 0) or 0)
    if policy <= 0:
        return False
    last_check = _parse_iso_timestamp(meta.get("last_live_check_at"))
    if last_check is None:
        return True
    return last_check + timedelta(seconds=policy) <= now


def _pick_auto_refresh_target(snapshot: dict, now: datetime) -> tuple[str | None, dict | None]:
    active_name = snapshot.get("active")
    if active_name:
        active_meta = snapshot.get("accounts", {}).get(active_name)
        if isinstance(active_meta, dict) and _should_auto_refresh_usage(active_meta, now):
            return active_name, active_meta
    for name, meta in snapshot.get("accounts", {}).items():
        if isinstance(meta, dict) and _should_auto_refresh_usage(meta, now):
            return name, meta
    return None, None


SORT_MODES = [
    ("added-oldest", "Added Oldest", "created_at", False),
    ("added-newest", "Added Newest", "created_at", True),
    ("usage-high", "Usage High", "usage", True),
    ("usage-low", "Usage Low", "usage", False),
    ("countdown-short", "Countdown Short", "countdown", False),
    ("countdown-long", "Countdown Long", "countdown", True),
]


def _sort_value(name: str, meta: dict, mode_key: str):
    if mode_key == "created_at":
        dt = _parse_iso_timestamp(meta.get("created_at"))
        return dt.timestamp() if dt else float("-inf")
    if mode_key == "usage":
        windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
        short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
        usage_value = short.get("value") if isinstance(short, dict) else meta.get("usage_value")
        if usage_value is None:
            return None
        try:
            return float(usage_value)
        except (TypeError, ValueError):
            return None
    if mode_key == "countdown":
        now = datetime.now(timezone.utc)
        windows = meta.get("usage_windows") if isinstance(meta.get("usage_windows"), dict) else {}
        short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
        target = _parse_iso_timestamp(short.get("reset_at")) or _parse_iso_timestamp(meta.get("reset_at")) or _parse_iso_timestamp(meta.get("cooldown_until"))
        if not target:
            return None
        return max(0.0, (target - now).total_seconds())
    return name.lower()


def _sorted_accounts(snapshot: dict, sort_mode_idx: int):
    mode_name, _label, mode_key, reverse = SORT_MODES[sort_mode_idx]
    accounts = list(snapshot["accounts"].items())
    known = []
    unknown = []
    for item in accounts:
        name, meta = item
        primary = _sort_value(name, meta, mode_key)
        if primary is None:
            unknown.append(item)
        else:
            known.append((primary, name.lower(), item))
    known.sort(key=lambda row: (row[0], row[1]), reverse=reverse)
    return mode_name, [row[2] for row in known] + sorted(unknown, key=lambda item: item[0].lower())


def _run_dashboard_terminal_action(stdscr, fn):
    curses.def_prog_mode()
    curses.endwin()
    try:
        return fn()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        curses.reset_prog_mode()
        stdscr.refresh()


def _dashboard_login(paths) -> str:
    print("\n[agy-cli-manager] Login Account\n")
    name = prompt_nonempty("Account name")
    agy_binary = input("agy binary [auto]: ").strip() or None
    timeout_raw = input("timeout seconds [600]: ").strip() or "600"
    stored_name = run_login_with_prompt(paths, name, agy_binary, int(timeout_raw))
    return f"{'logged-in' if stored_name else 'cancelled'}: {stored_name or name}"


def _dashboard_import(paths) -> str:
    print("\n[agy-cli-manager] Import Current/Live Profile\n")
    name = prompt_nonempty("Account name")
    source_dir = prompt_optional_path("Source dir")
    import_current(paths, name, source_dir)
    return f"imported-current: {name}"


def _dashboard(stdscr, paths) -> int:
    _init_dashboard_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    refresh_options = [5, 10, 15, 30]
    refresh_idx = 0
    selected_idx = 0
    sort_idx = 0
    message = "Live status refresh runs on due timers and manual refresh."
    snapshot = _refresh_dashboard_snapshot(paths)
    last_refresh = 0.0
    painted_once = False
    pending_auto_refresh_name = None
    refresh_result_queue: SimpleQueue = SimpleQueue()
    refresh_thread: threading.Thread | None = None
    refresh_inflight_name: str | None = None
    refresh_backoff_until: dict[str, float] = {}

    while True:
        try:
            while True:
                refresh_event = refresh_result_queue.get_nowait()
                refresh_inflight_name = None
                refresh_thread = None
                account_name = refresh_event["account"]
                if refresh_event.get("ok"):
                    refresh_backoff_until.pop(account_name, None)
                    snapshot = _refresh_dashboard_snapshot(paths)
                    last_refresh = time.time()
                    short_value = refresh_event.get("short_usage_value")
                    weekly_value = refresh_event.get("weekly_usage_value")
                    short_text = "-" if short_value is None else f"{short_value:.2f}%"
                    weekly_text = "-" if weekly_value is None else f"{weekly_value:.2f}%"
                    message = f"Background refreshed {account_name}: {short_text}/{weekly_text}"
                else:
                    refresh_backoff_until[account_name] = time.time() + 60
                    message = f"Background refresh failed for {account_name}: {refresh_event.get('error', 'unknown error')}"
        except Empty:
            pass

        now = time.time()
        interval = refresh_options[refresh_idx]
        if now - last_refresh >= interval or last_refresh == 0.0:
            snapshot = _refresh_dashboard_snapshot(paths)
            last_refresh = now
            auto_now_dt = datetime.now(timezone.utc)
            auto_target_name, _auto_target_meta = _pick_auto_refresh_target(snapshot, auto_now_dt)
            if painted_once:
                if auto_target_name and refresh_backoff_until.get(auto_target_name, 0.0) > now:
                    pending_auto_refresh_name = None
                else:
                    pending_auto_refresh_name = auto_target_name

        selected_name_hint = None
        if snapshot["accounts"]:
            raw_accounts = _sorted_accounts(snapshot, sort_idx)[1]
            if 0 <= selected_idx < len(raw_accounts):
                selected_name_hint = raw_accounts[selected_idx][0]
        sort_mode_name, accounts = _sorted_accounts(snapshot, sort_idx)
        if selected_name_hint:
            for idx, (name, _meta) in enumerate(accounts):
                if name == selected_name_hint:
                    selected_idx = idx
                    break
        if selected_idx >= len(accounts):
            selected_idx = max(0, len(accounts) - 1)

        stdscr.erase()
        height, width = stdscr.getmaxyx()

        top = (
            "AGY CLI Manager"
            f" | Active: {snapshot.get('active') or '-'}"
            f" | Accounts: {len(accounts)}"
            f" | UI Refresh: {interval}s"
            f" | Sort: {sort_mode_name}"
            " | Live Status: Auto+Manual"
        )
        top_lines = _draw_wrapped_lines(stdscr, 0, top, _color_attr(COLOR_HEADER, curses.A_BOLD))
        action_y = top_lines
        action_lines = _draw_action_bar(stdscr, action_y)
        legend_y = action_y + action_lines
        legend_lines = _draw_legend(stdscr, legend_y)
        divider_y = legend_y + legend_lines
        _draw_hline(stdscr, divider_y, "=")

        header_y = divider_y + 1
        _safe_addstr(stdscr, header_y, 0, "Accounts", _color_attr(COLOR_SECTION, curses.A_BOLD))
        columns = "Sel Name                        State      Usage        Reset In     Next Ref  Fail  Last Error"
        _safe_addstr(stdscr, header_y + 1, 0, columns, _color_attr(COLOR_SECTION, curses.A_BOLD | curses.A_UNDERLINE))

        detail_start = max(header_y + 8, min(height - 9, header_y + 3 + len(accounts)))
        list_rows = max(1, detail_start - (header_y + 2))
        scroll_offset = 0
        if selected_idx >= list_rows:
            scroll_offset = selected_idx - list_rows + 1

        now_dt = datetime.now(timezone.utc)
        visible_accounts = accounts[scroll_offset : scroll_offset + list_rows]
        for row_offset, (name, meta) in enumerate(visible_accounts):
            y = header_y + 2 + row_offset
            selected = scroll_offset + row_offset == selected_idx
            state = meta.get("status", "standby")
            usage = _format_usage(meta)
            reset_in = _format_countdown(meta, now_dt)
            next_refresh = _format_next_refresh(meta, now_dt)
            fail = str(int(meta.get("fail_count", 0) or 0))
            last_error = _format_last_error(meta)[:18]
            fail_attr = _severity_attr("bad" if int(meta.get("fail_count", 0) or 0) > 0 else "muted", selected)
            error_attr = _severity_attr("bad" if last_error != "-" else "muted", selected)
            marker = ">" if selected else ("*" if state == "active" else ".")
            _draw_segments(
                stdscr,
                y,
                [
                    (f"{marker:>3} ", _selected_marker_attr(selected)),
                    (f"{name[:28]:28}", _selected_name_attr(state, selected)),
                    ("  ", 0),
                    (f"{state[:9]:9}", _state_attr(state, selected)),
                    ("  ", 0),
                    (f"{usage[:12]:12}", _usage_attr(meta, selected)),
                    ("  ", 0),
                    (f"{reset_in[:12]:12}", _reset_attr(meta, now_dt, selected)),
                    ("  ", 0),
                    (f"{next_refresh[:8]:8}", _next_refresh_attr(meta, now_dt, selected)),
                    ("  ", 0),
                    (f"{fail:4}", fail_attr),
                    ("  ", 0),
                    (last_error, error_attr),
                ],
            )

        _draw_hline(stdscr, detail_start, "=")

        if accounts:
            selected_name, selected_meta = accounts[selected_idx]
            overview_rows = [
                ("Account", selected_name, _selected_name_attr(selected_meta.get("status", "standby"), True)),
                ("Mode", f"{selected_meta.get('status', 'standby')} | {'enabled' if selected_meta.get('enabled', True) else 'disabled'}", _detail_value_attr(selected_meta, "State", now_dt)),
                ("Health", _format_live_state(selected_meta, now_dt), _detail_value_attr(selected_meta, "Health", now_dt)),
                ("Usage", _format_usage(selected_meta), _usage_attr(selected_meta)),
            ]
            left_rows = [
                ("Identity", _format_identity(selected_meta), _detail_value_attr(selected_meta, "Identity", now_dt)),
                ("Failures", str(int(selected_meta.get('fail_count', 0) or 0)), _detail_value_attr(selected_meta, "Failures", now_dt)),
                ("Cooldown", selected_meta.get('cooldown_until') or '-', _detail_value_attr(selected_meta, "Cooldown Until", now_dt)),
                ("Added", selected_meta.get('created_at') or '-', _severity_attr("muted")),
                ("Last Check", _format_age(selected_meta.get('last_live_check_at'), now_dt), _severity_attr("info")),
                ("Next Refresh", f"{_format_next_refresh(selected_meta, now_dt)} | {int(selected_meta.get('refresh_policy_seconds', 0) or 0)}s policy", _detail_value_attr(selected_meta, "Next Refresh", now_dt)),
            ]
            right_rows = [
                ("Short Window", _format_window_summary(selected_meta, 'short', now_dt), _detail_value_attr(selected_meta, "Short Window", now_dt)),
                ("Weekly Window", _format_window_summary(selected_meta, 'weekly', now_dt), _detail_value_attr(selected_meta, "Weekly Window", now_dt)),
                ("Live Error", selected_meta.get('last_live_check_error') or '-', _detail_value_attr(selected_meta, "Last Live Error", now_dt)),
                ("Last Error", _format_last_error(selected_meta), _detail_value_attr(selected_meta, "Last Error", now_dt)),
                ("Policy", "auto on due + manual refresh", _severity_attr("info")),
            ]
        else:
            overview_rows = [
                ("Status", "No saved accounts.", _severity_attr("muted")),
                ("Hint", "Add one with login/import-current.", _severity_attr("info")),
            ]
            left_rows = []
            right_rows = []

        _safe_addstr(stdscr, detail_start + 1, 0, "Overview", _color_attr(COLOR_SECTION, curses.A_BOLD))
        overview_x = 0
        for idx, (label, value, value_attr) in enumerate(overview_rows):
            _draw_detail_line_at(stdscr, detail_start + 2, overview_x, label, value, value_attr)
            overview_x += max(24, len(label) + len(value) + 6)

        detail_block_y = detail_start + 4
        available_width = max(20, width - 1)
        use_two_columns = available_width >= 110
        if use_two_columns:
            split_x = max(32, available_width // 2)
            _draw_detail_block(stdscr, detail_block_y, "Account", left_rows, 0)
            _draw_detail_block(stdscr, detail_block_y, "Quota", right_rows, split_x)
        else:
            used_left = _draw_detail_block(stdscr, detail_block_y, "Account", left_rows, 0)
            _draw_detail_block(stdscr, detail_block_y + used_left + 1, "Quota", right_rows, 0)

        _draw_hline(stdscr, height - 2, "=")
        _safe_addstr(stdscr, height - 1, 0, f"Status: {message}"[: max(0, width - 1)], _message_attr(message))
        stdscr.refresh()

        if not painted_once:
            painted_once = True
            auto_now_dt = datetime.now(timezone.utc)
            auto_target_name, _auto_target_meta = _pick_auto_refresh_target(snapshot, auto_now_dt)
            if auto_target_name and refresh_backoff_until.get(auto_target_name, 0.0) <= time.time():
                pending_auto_refresh_name = auto_target_name
        if pending_auto_refresh_name:
            auto_target_name = pending_auto_refresh_name
            pending_auto_refresh_name = None
            if refresh_inflight_name is None and refresh_backoff_until.get(auto_target_name, 0.0) <= time.time():
                refresh_thread = _start_usage_refresh_worker(paths, auto_target_name, refresh_result_queue)
                refresh_inflight_name = auto_target_name
                message = f"Background refreshing {auto_target_name}..."

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return 130

        if key == -1:
            time.sleep(0.1)
            continue

        if key in (ord("q"), ord("Q")):
            return 0
        if key in (curses.KEY_UP, ord("k"), ord("K")) and selected_idx > 0:
            selected_idx -= 1
            continue
        if key in (curses.KEY_DOWN, ord("j"), ord("J")) and selected_idx < max(0, len(accounts) - 1):
            selected_idx += 1
            continue
        if key in (ord("t"), ord("T")):
            refresh_idx = (refresh_idx + 1) % len(refresh_options)
            message = f"UI refresh set to {refresh_options[refresh_idx]}s."
            continue
        if key in (ord("s"), ord("S")):
            sort_idx = (sort_idx + 1) % len(SORT_MODES)
            message = f"Sort set to {SORT_MODES[sort_idx][1]}."
            continue
        if key in (ord("n"), ord("N")):
            try:
                message = _run_dashboard_terminal_action(stdscr, lambda: _dashboard_login(paths))
            except (ValueError, KeyboardInterrupt) as exc:
                message = "Cancelled." if isinstance(exc, KeyboardInterrupt) else f"Error: {exc}"
            snapshot = _refresh_dashboard_snapshot(paths)
            last_refresh = time.time()
            continue
        if key in (ord("i"), ord("I")):
            try:
                message = _run_dashboard_terminal_action(stdscr, lambda: _dashboard_import(paths))
            except (ValueError, KeyboardInterrupt) as exc:
                message = "Cancelled." if isinstance(exc, KeyboardInterrupt) else f"Error: {exc}"
            snapshot = _refresh_dashboard_snapshot(paths)
            last_refresh = time.time()
            continue
        if not accounts:
            message = "No accounts available for this action."
            continue

        selected_name, selected_meta = accounts[selected_idx]

        try:
            if key in (ord("u"), ord("U")):
                if refresh_inflight_name is not None:
                    message = f"Refresh already running for {refresh_inflight_name}."
                else:
                    refresh_backoff_until.pop(selected_name, None)
                    refresh_thread = _start_usage_refresh_worker(paths, selected_name, refresh_result_queue)
                    refresh_inflight_name = selected_name
                    message = f"Background refreshing {selected_name}..."
            elif key in (10, 13, curses.KEY_ENTER, ord("a"), ord("A")):
                previous = switch_account(paths, selected_name)
                message = f"Activated {selected_name}." if previous != selected_name else f"{selected_name} already active."
            elif key in (ord("r"), ord("R")):
                target = switch_next(paths)
                message = f"Rotated to {target}."
            elif key in (ord("e"), ord("E")):
                enabled = bool(selected_meta.get("enabled", True))
                set_enabled(paths, selected_name, not enabled)
                message = f"{'Enabled' if not enabled else 'Disabled'} {selected_name}."
            elif key in (ord("c"), ord("C")):
                clear_bad(paths, selected_name)
                message = f"Cleared bad state for {selected_name}."
            elif key in (ord("m"), ord("M")):
                mark_bad(paths, selected_name, "manual", 60)
                message = f"Marked {selected_name} bad with 60m cooldown."
            else:
                message = "Unknown key."
                continue
            snapshot = _refresh_dashboard_snapshot(paths)
            last_refresh = time.time()
        except ValueError as exc:
            message = f"Error: {exc}"

    return 0


def run_dashboard(paths) -> int:
    ensure_layout(paths)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Dashboard requires an interactive TTY.")
    return curses.wrapper(_dashboard, paths)


def print_account_list(paths, as_json: bool) -> None:
    snapshot = get_status_snapshot(paths)
    accounts = []
    for name, meta in snapshot["accounts"].items():
        accounts.append(
            {
                "name": name,
                "status": meta.get("status"),
                "enabled": bool(meta.get("enabled", True)),
                "identity": meta.get("identity"),
                "last_error": meta.get("last_error"),
                "cooldown_until": meta.get("cooldown_until"),
                "fail_count": int(meta.get("fail_count", 0) or 0),
            }
        )
    if as_json:
        print(json.dumps({"active": snapshot.get("active"), "accounts": accounts}, indent=2, sort_keys=True))
        return
    if not accounts:
        print("no-accounts")
        return
    for entry in accounts:
        marker = "*" if entry["name"] == snapshot.get("active") else "-"
        status = entry["status"] or "standby"
        enabled = "enabled" if entry["enabled"] else "disabled"
        print(f"{marker} {entry['name']} [{status}, {enabled}]")


def print_current_account(paths, as_json: bool) -> None:
    snapshot = get_status_snapshot(paths)
    active = snapshot.get("active")
    if as_json:
        print(json.dumps({"active": active}, indent=2, sort_keys=True))
        return
    print(active or "-")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    paths = build_paths(args.root)

    try:
        if args.command is None:
            return run_dashboard(paths)
        if args.command == "dashboard":
            return run_dashboard(paths)
        if args.command == "menu":
            return run_menu(paths, parser)
        if args.command == "init":
            ensure_layout(paths)
            print(f"initialized: {paths.root}")
            return 0
        if args.command == "status":
            if args.json:
                print(json.dumps(get_status_snapshot(paths), indent=2, sort_keys=True))
            else:
                print(format_status(paths))
            return 0
        if args.command == "current":
            print_current_account(paths, args.json)
            return 0
        if args.command == "list":
            print_account_list(paths, args.json)
            return 0
        if args.command == "whoami":
            if args.refresh and args.name:
                resolved_name = args.name
                identity = refresh_account_identity(paths, args.name)
            elif args.refresh:
                resolved_name, _ = get_account_identity(paths)
                identity = refresh_account_identity(paths, resolved_name)
            else:
                resolved_name, identity = get_account_identity(paths, args.name)
            print(f"account: {resolved_name}")
            print(f"account_name: {identity.get('account_name') or '-'}")
            print(f"source: {identity.get('source') or '-'}")
            if identity.get("display_name"):
                print(f"display_name: {identity['display_name']}")
            if identity.get("email"):
                print(f"email: {identity['email']}")
            if args.probe_usage:
                if args.name:
                    source_dir = paths.accounts_dir / args.name
                else:
                    source_dir = paths.runtime_dir
                live_dir = get_live_dir(load_state(paths))
                probe = probe_profile_identity_via_usage(
                    source_dir,
                    args.agy_binary,
                    args.timeout_seconds,
                    live_dir=live_dir,
                )
                print(f"usage_account_name: {probe.get('account_name') or '-'}")
                print(f"usage_source: {probe.get('source') or '-'}")
                if probe.get("raw_hint"):
                    print("usage_hint:")
                    print(probe["raw_hint"])
            return 0
        if args.command == "apply-active":
            active = apply_active(paths)
            print(f"applied-active: {active}")
            return 0
        if args.command == "refresh-usage":
            result = refresh_account_usage(
                paths,
                args.name,
                agy_binary=args.agy_binary,
                warmup_timeout_seconds=args.warmup_timeout_seconds,
            )
            payload = {
                "account": result.account,
                "source_home": result.source_home,
                "project_id": result.project_id,
                "plan_type": result.plan_type,
                "prompt_credits_available": result.prompt_credits_available,
                "prompt_credits_monthly": result.prompt_credits_monthly,
                "short_usage_status": result.short_usage_status,
                "short_usage_value": result.short_usage_value,
                "short_reset_at": result.short_reset_at,
                "weekly_usage_status": result.weekly_usage_status,
                "weekly_usage_value": result.weekly_usage_value,
                "weekly_reset_at": result.weekly_reset_at,
                "bucket_count": result.bucket_count,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                short_value = "-" if result.short_usage_value is None else f"{result.short_usage_value:.2f}%"
                print(
                    f"refreshed-usage: {result.account} short={short_value} "
                    f"reset_at={result.short_reset_at or '-'} buckets={result.bucket_count}"
                )
            return 0
        if args.command == "add":
            add_account(paths, args.name, args.source_dir)
            print(f"added: {args.name}")
            return 0
        if args.command == "import-current":
            import_current(paths, args.name, args.source_dir)
            print(f"imported-current: {args.name}")
            return 0
        if args.command == "login":
            name = args.name or prompt_nonempty("Account name")
            stored_name = run_login_with_prompt(paths, name, args.agy_binary, args.timeout_seconds)
            print(f"{'logged-in' if stored_name else 'cancelled'}: {stored_name or name}")
            return 0
        if args.command == "switch":
            previous = switch_account(paths, args.name)
            if previous:
                print(f"switched: {previous} -> {args.name}")
            else:
                print(f"switched: {args.name}")
            return 0
        if args.command == "activate":
            previous = switch_account(paths, args.name)
            if previous:
                print(f"activated: {previous} -> {args.name}")
            else:
                print(f"activated: {args.name}")
            return 0
        if args.command == "switch-next":
            target = switch_next(paths)
            print(f"switched-next: {target}")
            return 0
        if args.command == "rotate":
            target = switch_next(paths)
            if args.json:
                print(json.dumps({"active": target}, indent=2, sort_keys=True))
            else:
                print(f"rotated: {target}")
            return 0
        if args.command == "disable":
            set_enabled(paths, args.name, False)
            print(f"disabled: {args.name}")
            return 0
        if args.command == "enable":
            set_enabled(paths, args.name, True)
            print(f"enabled: {args.name}")
            return 0
        if args.command == "mark-bad":
            mark_bad(paths, args.name, args.reason, args.cooldown_minutes)
            print(f"marked-bad: {args.name}")
            return 0
        if args.command == "clear-bad":
            clear_bad(paths, args.name)
            print(f"cleared-bad: {args.name}")
            return 0
        if args.command == "set-live-dir":
            live_dir = Path(args.path).expanduser() if args.path else None
            set_live_dir(paths, live_dir)
            print(f"live-dir: {live_dir if live_dir else 'cleared'}")
            return 0
        if args.command == "rotate-after-failure":
            live_dir = Path(args.live_dir).expanduser() if args.live_dir else None
            result = rotate_after_failure(
                paths,
                reason=args.reason,
                cooldown_minutes=args.cooldown_minutes,
                live_dir=live_dir,
            )
            if args.json:
                print(json.dumps({
                    "previous_active": result.previous_active,
                    "active": result.active,
                    "switched_to": result.switched_to,
                    "marked_bad": result.marked_bad,
                    "reason": result.reason,
                    "cooldown_minutes": result.cooldown_minutes,
                }, indent=2, sort_keys=True))
            else:
                if result.previous_active and result.switched_to:
                    print(f"rotated: {result.previous_active} -> {result.switched_to}")
                elif result.previous_active:
                    print(f"marked-bad-no-standby: {result.previous_active}")
                else:
                    print("no-active-account")
            return 0
        if args.command == "update-meta":
            meta = update_account_runtime_metadata(
                paths,
                args.name,
                usage_status=args.usage_status,
                usage_value=args.usage_value,
                reset_at=args.reset_at,
                short_usage_status=args.short_usage_status,
                short_usage_value=args.short_usage_value,
                short_reset_at=args.short_reset_at,
                weekly_usage_status=args.weekly_usage_status,
                weekly_usage_value=args.weekly_usage_value,
                weekly_reset_at=args.weekly_reset_at,
                health_status=args.health_status,
                last_live_check_at=args.last_live_check_at,
                last_live_check_error=args.last_live_check_error,
                next_live_check_at=args.next_live_check_at,
                refresh_policy_seconds=args.refresh_policy_seconds,
            )
            if args.json:
                print(json.dumps(meta, indent=2, sort_keys=True))
            else:
                print(f"updated-meta: {args.name}")
            return 0
    except ValueError as e:
        parser.exit(2, f"error: {e}\n")
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    parser.exit(2, "error: unknown command\n")


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import curses
import json
import textwrap
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
    refresh_account_identity,
    rotate_after_failure,
    set_live_dir,
    set_enabled,
    switch_account,
    switch_next,
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


def _refresh_dashboard_snapshot(paths):
    return get_status_snapshot(paths)


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
    usage_value = meta.get("usage_value")
    usage_status = meta.get("usage_status") or "unknown"
    if usage_value is not None:
        return str(usage_value)[:7]
    if usage_status == "unknown":
        return "-"
    return usage_status[:7]


def _format_countdown(meta: dict, now: datetime) -> str:
    target = _parse_iso_timestamp(meta.get("reset_at")) or _parse_iso_timestamp(meta.get("cooldown_until"))
    if not target:
        return "-"
    delta = int((target - now).total_seconds())
    if delta <= 0:
        return "0s"
    minutes, seconds = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02}m"
    if minutes:
        return f"{minutes}m{seconds:02}s"
    return f"{seconds}s"


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
        usage_value = meta.get("usage_value")
        if usage_value is None:
            return None
        try:
            return float(usage_value)
        except (TypeError, ValueError):
            return None
    if mode_key == "countdown":
        now = datetime.now(timezone.utc)
        target = _parse_iso_timestamp(meta.get("reset_at")) or _parse_iso_timestamp(meta.get("cooldown_until"))
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
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    refresh_options = [5, 10, 15, 30]
    refresh_idx = 0
    selected_idx = 0
    sort_idx = 0
    message = "Live status refresh is manual only."
    snapshot = _refresh_dashboard_snapshot(paths)
    last_refresh = 0.0

    while True:
        now = time.time()
        interval = refresh_options[refresh_idx]
        if now - last_refresh >= interval or last_refresh == 0.0:
            snapshot = _refresh_dashboard_snapshot(paths)
            last_refresh = now

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
            " | Live Status: Manual"
        )
        top_lines = _draw_wrapped_lines(stdscr, 0, top, curses.A_BOLD)
        actions = "Actions: [N] Login [I] Import [Enter/A] Activate [R]otate [E]nable/Disable [C]learBad [M]arkBad [S] Sort [U] Refresh [T] UI Refresh [Q] Quit"
        action_y = top_lines
        action_lines = _draw_wrapped_lines(stdscr, action_y, actions)
        divider_y = action_y + action_lines
        _draw_hline(stdscr, divider_y, "=")

        header_y = divider_y + 1
        _safe_addstr(stdscr, header_y, 0, "Accounts", curses.A_BOLD)
        columns = "Name                          State      Usage    Reset In  Fail  Last Error"
        _safe_addstr(stdscr, header_y + 1, 0, columns, curses.A_UNDERLINE)

        detail_start = max(header_y + 8, min(height - 9, header_y + 3 + len(accounts)))
        list_rows = max(1, detail_start - (header_y + 2))
        scroll_offset = 0
        if selected_idx >= list_rows:
            scroll_offset = selected_idx - list_rows + 1

        now_dt = datetime.now(timezone.utc)
        visible_accounts = accounts[scroll_offset : scroll_offset + list_rows]
        for row_offset, (name, meta) in enumerate(visible_accounts):
            y = header_y + 2 + row_offset
            state = meta.get("status", "standby")
            usage = _format_usage(meta)
            reset_in = _format_countdown(meta, now_dt)
            fail = str(int(meta.get("fail_count", 0) or 0))
            last_error = _format_last_error(meta)[:24]
            line = f"{name[:28]:28}  {state[:9]:9}  {usage[:7]:7}  {reset_in[:8]:8}  {fail:4}  {last_error}"
            attr = curses.A_REVERSE if scroll_offset + row_offset == selected_idx else 0
            _safe_addstr(stdscr, y, 0, line, attr)

        _draw_hline(stdscr, detail_start, "=")
        _safe_addstr(stdscr, detail_start + 1, 0, "Selected Account", curses.A_BOLD)

        if accounts:
            selected_name, selected_meta = accounts[selected_idx]
            detail_lines = [
                f"Name: {selected_name}",
                f"State: {selected_meta.get('status', 'standby')} | Enabled: {'yes' if selected_meta.get('enabled', True) else 'no'}",
                f"Identity: {_format_identity(selected_meta)}",
                f"Failures: {int(selected_meta.get('fail_count', 0) or 0)}",
                f"Cooldown Until: {selected_meta.get('cooldown_until') or '-'}",
                f"Added At: {selected_meta.get('created_at') or '-'}",
                f"Usage: {_format_usage(selected_meta)} | Reset In: {_format_countdown(selected_meta, now_dt)}",
                f"Last Error: {_format_last_error(selected_meta)}",
                f"Live Check Policy: manual only",
            ]
        else:
            detail_lines = [
                "No saved accounts.",
                "Use `agy-cli-manager login` or `agy-cli-manager import-current` to add one.",
            ]

        for idx, line in enumerate(detail_lines):
            _safe_addstr(stdscr, detail_start + 2 + idx, 0, line)

        _draw_hline(stdscr, height - 2, "=")
        _safe_addstr(stdscr, height - 1, 0, f"Status: {message}"[: max(0, width - 1)])
        stdscr.refresh()

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
        if key in (ord("u"), ord("U")):
            snapshot = _refresh_dashboard_snapshot(paths)
            last_refresh = time.time()
            message = "Local state refreshed."
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
            if key in (10, 13, curses.KEY_ENTER, ord("a"), ord("A")):
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
    except ValueError as e:
        parser.exit(2, f"error: {e}\n")
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    parser.exit(2, "error: unknown command\n")


if __name__ == "__main__":
    raise SystemExit(main())

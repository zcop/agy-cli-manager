from __future__ import annotations

import argparse
from pathlib import Path

from agy_cli_manager.manager import (
    add_account,
    apply_active,
    build_paths,
    clear_bad,
    default_root,
    ensure_layout,
    format_status,
    mark_bad,
    set_live_dir,
    set_enabled,
    switch_account,
    switch_next,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agy-cli-manager")
    parser.add_argument("--root", type=Path, default=default_root(), help="Manager root directory")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create initial manager layout")
    sub.add_parser("status", help="Show current manager status")
    sub.add_parser("apply-active", help="Re-apply the current active account to runtime and live_dir")

    add = sub.add_parser("add", help="Add an account profile from a source directory")
    add.add_argument("name")
    add.add_argument("source_dir", type=Path)

    switch = sub.add_parser("switch", help="Switch to a named account")
    switch.add_argument("name")

    sub.add_parser("switch-next", help="Switch to the next enabled standby account")

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
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    paths = build_paths(args.root)

    try:
        if args.command == "init":
            ensure_layout(paths)
            print(f"initialized: {paths.root}")
            return 0
        if args.command == "status":
            print(format_status(paths))
            return 0
        if args.command == "apply-active":
            active = apply_active(paths)
            print(f"applied-active: {active}")
            return 0
        if args.command == "add":
            add_account(paths, args.name, args.source_dir)
            print(f"added: {args.name}")
            return 0
        if args.command == "switch":
            previous = switch_account(paths, args.name)
            if previous:
                print(f"switched: {previous} -> {args.name}")
            else:
                print(f"switched: {args.name}")
            return 0
        if args.command == "switch-next":
            target = switch_next(paths)
            print(f"switched-next: {target}")
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
    except ValueError as e:
        parser.exit(2, f"error: {e}\n")

    parser.exit(2, "error: unknown command\n")


if __name__ == "__main__":
    raise SystemExit(main())

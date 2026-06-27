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
    import_current,
    login_account,
    mark_bad,
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
    sub.add_parser("menu", help="Open the interactive menu")
    sub.add_parser("status", help="Show current manager status")
    sub.add_parser("apply-active", help="Re-apply the current active account to runtime and live_dir")

    add = sub.add_parser("add", help="Add an account profile from a source directory")
    add.add_argument("name")
    add.add_argument("source_dir", type=Path)

    import_cmd = sub.add_parser("import-current", help="Import the current live_dir or a provided source dir as an account")
    import_cmd.add_argument("name")
    import_cmd.add_argument("source_dir", type=Path, nargs="?")

    login = sub.add_parser("login", help="Run isolated agy login and save the resulting profile")
    login.add_argument("name", nargs="?")
    login.add_argument("--agy-binary", default="agy")
    login.add_argument("--timeout-seconds", type=int, default=180)

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
        print("0. Exit")

        choice = input("Select: ").strip()
        try:
            if choice == "1":
                print(format_status(paths))
            elif choice == "2":
                name = prompt_nonempty("Account name")
                agy_binary = input("agy binary [agy]: ").strip() or "agy"
                timeout_raw = input("timeout seconds [180]: ").strip() or "180"
                completed = login_account(paths, name, agy_binary, int(timeout_raw))
                print(f"{'logged-in' if completed else 'cancelled'}: {name}")
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
            elif choice == "0":
                return 0
            else:
                print("Unknown selection.")
        except ValueError as e:
            print(f"error: {e}")
        except KeyboardInterrupt:
            print("\nCancelled.")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    paths = build_paths(args.root)

    try:
        if args.command in (None, "menu"):
            return run_menu(paths, parser)
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
        if args.command == "import-current":
            import_current(paths, args.name, args.source_dir)
            print(f"imported-current: {args.name}")
            return 0
        if args.command == "login":
            name = args.name or prompt_nonempty("Account name")
            completed = login_account(paths, name, args.agy_binary, args.timeout_seconds)
            print(f"{'logged-in' if completed else 'cancelled'}: {name}")
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
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130

    parser.exit(2, "error: unknown command\n")


if __name__ == "__main__":
    raise SystemExit(main())

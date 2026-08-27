from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from agy_cli_manager.manager import (
    add_account,
    build_paths,
    ensure_layout,
    get_status_snapshot,
    load_state,
    manager_lock,
    save_state,
    set_live_dir,
    set_switch_mode,
    utc_now,
)
from agy_cli_manager.watch import (
    clear_restart_required,
    consume_log_events,
    iter_live_agy_log_files,
    load_log_watch_state,
    log_watch_state_path,
    parse_quota_log_line,
    poll_quota_logs,
    read_new_complete_lines,
    resolve_antigravity_cli_dir,
)


SAMPLE_QUOTA_LINE = (
    "ERROR: logging before google.Init: I0827 06:00:00.000000  12345 run.go:371] "
    "Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
    "Please upgrade your subscription to increase your limits. Resets in ~2h.), retrying in 1s"
)
SAMPLE_ERRORREPORT_LINE = (
    "ERROR: logging before google.Init: E0827 06:00:01.000000  12345 errorreport.go:223] "
    "agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
    "Please upgrade your subscription to increase your limits. Resets in 1h45m7s."
)
SAMPLE_TUI_LINE = (
    "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in ~2h"
)


class ParseQuotaLogLineTests(unittest.TestCase):
    def test_parses_resource_exhausted_individual_quota(self) -> None:
        event = parse_quota_log_line(SAMPLE_QUOTA_LINE)
        assert event is not None
        self.assertEqual(event.kind, "individual_quota")
        self.assertEqual(event.reset_hint, "~2h")

    def test_parses_errorreport_quota_line(self) -> None:
        event = parse_quota_log_line(SAMPLE_ERRORREPORT_LINE)
        assert event is not None
        self.assertEqual(event.kind, "individual_quota")
        self.assertEqual(event.reset_hint, "1h45m7s")

    def test_parses_bare_tui_banner(self) -> None:
        event = parse_quota_log_line(SAMPLE_TUI_LINE)
        assert event is not None
        self.assertEqual(event.kind, "individual_quota")
        self.assertEqual(event.reset_hint, "~2h")

    def test_parses_weekly_quota(self) -> None:
        event = parse_quota_log_line("weekly quota reached for this account")
        assert event is not None
        self.assertEqual(event.kind, "weekly_quota")

    def test_ignores_unrelated_429(self) -> None:
        self.assertIsNone(parse_quota_log_line("RESOURCE_EXHAUSTED (code 429): Rate limit exceeded"))

    def test_ignores_quota_manager_refresh(self) -> None:
        self.assertIsNone(parse_quota_log_line("quota_manager.go:45] doRefreshQuota: starting reload (force=false)"))

    def test_ignores_blank_and_noise(self) -> None:
        self.assertIsNone(parse_quota_log_line(""))
        self.assertIsNone(parse_quota_log_line("Language server listening on random port"))


class LiveLogDiscoveryTests(unittest.TestCase):
    def test_discovers_cli_log_and_session_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            cli_log = live_dir / "antigravity-cli" / "cli.log"
            session_log = log_dir / "cli-20260827_084029.log"
            cli_log.write_text("boot\n", encoding="utf-8")
            session_log.write_text("session\n", encoding="utf-8")
            self.assertEqual(resolve_antigravity_cli_dir(live_dir), live_dir / "antigravity-cli")
            discovered = {path.name for path in iter_live_agy_log_files(live_dir)}
            self.assertEqual(discovered, {"cli.log", "cli-20260827_084029.log"})

    def test_accepts_home_root_containing_dot_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            log_dir = home / ".gemini" / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            (log_dir / "cli-1.log").write_text("x\n", encoding="utf-8")
            discovered = iter_live_agy_log_files(home)
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].name, "cli-1.log")


class CursorAndConsumeTests(unittest.TestCase):
    def test_read_new_complete_lines_holds_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cli.log"
            path.write_bytes(b"complete\npartial")
            offset, lines = read_new_complete_lines(path, 0)
            self.assertEqual(lines, ["complete"])
            self.assertEqual(offset, len(b"complete\n"))
            path.write_bytes(b"complete\npartial\n")
            offset, lines = read_new_complete_lines(path, offset)
            self.assertEqual(lines, ["partial"])
            self.assertEqual(offset, path.stat().st_size)

    def test_truncated_file_rewinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cli.log"
            path.write_bytes(b"old line\n")
            offset, _lines = read_new_complete_lines(path, 0)
            path.write_bytes(b"new\n")
            _offset, lines = read_new_complete_lines(path, offset)
            self.assertEqual(lines, ["new"])

    def test_consume_skips_existing_bytes_then_sees_new_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "cli-session.log"
            log_path.write_text(SAMPLE_QUOTA_LINE + "\n", encoding="utf-8")
            cursors, events = consume_log_events(live_dir, {}, from_start=False, started_at=None)
            self.assertEqual(events, [])
            self.assertIn(str(log_path), cursors)

            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(SAMPLE_ERRORREPORT_LINE + "\n")
            _cursors, events = consume_log_events(live_dir, cursors, from_start=False, started_at=None)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "individual_quota")
            self.assertEqual(events[0].reset_hint, "1h45m7s")

    def test_from_start_replays_quota_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "cli-session.log"
            log_path.write_text(SAMPLE_QUOTA_LINE + "\nnoise\n", encoding="utf-8")
            _cursors, events = consume_log_events(live_dir, {}, from_start=True)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "individual_quota")

    def test_recently_modified_existing_log_starts_at_eof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "cli-session.log"
            log_path.write_text(SAMPLE_QUOTA_LINE + "\n", encoding="utf-8")
            now = time.time()
            os.utime(log_path, (now, now))
            cursors, events = consume_log_events(
                live_dir,
                {},
                from_start=False,
                started_at=now,
            )
            self.assertEqual(events, [])
            self.assertEqual(cursors[str(log_path)]["offset"], log_path.stat().st_size)

    def test_new_file_during_watch_is_read_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            existing = log_dir / "cli-existing.log"
            existing.write_text("boot\n", encoding="utf-8")
            cursors, events = consume_log_events(live_dir, {}, from_start=False)
            self.assertEqual(events, [])
            self.assertIn(str(existing), cursors)

            created = log_dir / "cli-new.log"
            created.write_text(SAMPLE_TUI_LINE + "\n", encoding="utf-8")
            _cursors, events = consume_log_events(
                live_dir,
                cursors,
                from_start=False,
                initialized=True,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "individual_quota")
            self.assertEqual(events[0].path, str(created))

    def test_first_log_created_after_empty_init_is_read_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            cursors, events = consume_log_events(
                live_dir,
                {},
                from_start=False,
                initialized=False,
            )
            self.assertEqual(events, [])
            self.assertEqual(cursors, {})

            log_path = log_dir / "cli-new.log"
            log_path.write_text(SAMPLE_TUI_LINE + "\n", encoding="utf-8")
            _cursors, events = consume_log_events(
                live_dir,
                cursors,
                from_start=False,
                initialized=True,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "individual_quota")
            self.assertEqual(events[0].path, str(log_path))


def _write_token_home(root: Path, name: str) -> Path:
    home = root / f"src-{name}"
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text(f"token-{name}", encoding="utf-8")
    return home


def _append_log(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


def _make_watch_harness(tmp: Path, names: tuple[str, ...] = ("account-a", "account-b", "account-c")):
    root = tmp / "manager"
    live_dir = tmp / "live" / ".gemini"
    log_dir = live_dir / "antigravity-cli" / "log"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "cli-session.log"
    log_path.write_text("boot\n", encoding="utf-8")

    paths = build_paths(root)
    ensure_layout(paths)
    set_live_dir(paths, live_dir)
    for name in names:
        add_account(paths, name, _write_token_home(tmp, name))
    set_switch_mode(paths, "auto")
    return paths, live_dir, log_path


def _expire_switch_dedupe(paths, seconds: int = 30) -> None:
    with manager_lock(paths):
        state = load_state(paths)
        runtime = dict(state.get("switch_runtime") or {})
        runtime["last_completed_at"] = (utc_now() - timedelta(seconds=seconds)).isoformat()
        runtime["status"] = "ready"
        runtime["reason"] = "quota"
        state["switch_runtime"] = runtime
        save_state(paths, state)


def _cooldown_names(paths) -> list[str]:
    snapshot = get_status_snapshot(paths)
    names = []
    for name, meta in snapshot.get("accounts", {}).items():
        if meta.get("status") == "cooldown":
            names.append(name)
    return sorted(names)


class LogWatchIntegrationTests(unittest.TestCase):
    def test_old_process_quota_does_not_rotate_after_dedupe_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, _live_dir, log_path = _make_watch_harness(Path(tmp))
            first = poll_quota_logs(paths, rotate=True)
            self.assertFalse(first.rotated)
            self.assertEqual(first.events, [])

            _append_log(log_path, SAMPLE_QUOTA_LINE)
            rotated = poll_quota_logs(paths, rotate=True)
            self.assertTrue(rotated.rotated)
            self.assertEqual(rotated.rotation.previous_active, "account-a")
            self.assertEqual(rotated.rotation.switched_to, "account-b")
            self.assertTrue(rotated.restart_required)
            self.assertEqual(get_status_snapshot(paths)["active"], "account-b")

            _expire_switch_dedupe(paths, seconds=30)
            _append_log(log_path, SAMPLE_ERRORREPORT_LINE)
            blocked = poll_quota_logs(paths, rotate=True)
            self.assertFalse(blocked.rotated)
            self.assertTrue(blocked.restart_required)
            self.assertIn("waiting for agy restart", blocked.message)
            snapshot = get_status_snapshot(paths)
            self.assertEqual(snapshot["active"], "account-b")
            self.assertEqual(_cooldown_names(paths), ["account-a"])
            self.assertEqual(snapshot["accounts"]["account-c"]["status"], "standby")

    def test_ack_restart_clears_restart_required_and_allows_next_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, _live_dir, log_path = _make_watch_harness(Path(tmp))
            poll_quota_logs(paths, rotate=True)
            _append_log(log_path, SAMPLE_QUOTA_LINE)
            rotated = poll_quota_logs(paths, rotate=True)
            self.assertTrue(rotated.restart_required)

            state = clear_restart_required(paths)
            self.assertFalse(state["restart_required"])
            self.assertIsNone(state["restart_armed_at"])
            snapshot = get_status_snapshot(paths)
            self.assertFalse(snapshot["log_watch"]["restart_required"])

            _expire_switch_dedupe(paths, seconds=30)
            _append_log(log_path, SAMPLE_TUI_LINE)
            after_ack = poll_quota_logs(paths, rotate=True)
            self.assertTrue(after_ack.rotated)
            self.assertEqual(after_ack.rotation.previous_active, "account-b")
            self.assertEqual(after_ack.rotation.switched_to, "account-c")
            self.assertTrue(after_ack.restart_required)

    def test_new_session_log_identifies_restart_without_pty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, live_dir, log_path = _make_watch_harness(Path(tmp))
            poll_quota_logs(paths, rotate=True)
            _append_log(log_path, SAMPLE_QUOTA_LINE)
            rotated = poll_quota_logs(paths, rotate=True)
            self.assertTrue(rotated.restart_required)

            new_log = live_dir / "antigravity-cli" / "log" / "cli-new-session.log"
            new_log.write_text("session start\n", encoding="utf-8")
            detected = poll_quota_logs(paths, rotate=True)
            self.assertFalse(detected.rotated)
            self.assertFalse(detected.restart_required)
            self.assertEqual(get_status_snapshot(paths)["active"], "account-b")

            _expire_switch_dedupe(paths, seconds=30)
            _append_log(log_path, SAMPLE_ERRORREPORT_LINE)
            old_process = poll_quota_logs(paths, rotate=True)
            self.assertFalse(old_process.rotated)
            self.assertEqual(get_status_snapshot(paths)["active"], "account-b")

            _append_log(new_log, SAMPLE_TUI_LINE)
            new_process = poll_quota_logs(paths, rotate=True)
            self.assertTrue(new_process.rotated)
            self.assertEqual(new_process.rotation.previous_active, "account-b")
            self.assertEqual(new_process.rotation.switched_to, "account-c")

    def test_concurrent_pollers_keep_cursors_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, _live_dir, log_path = _make_watch_harness(Path(tmp), names=("account-a",))
            seeded = poll_quota_logs(paths, rotate=False)
            self.assertFalse(seeded.rotated)
            errors: list[BaseException] = []

            def writer() -> None:
                try:
                    for index in range(40):
                        _append_log(log_path, f"noise {index}")
                        time.sleep(0.002)
                except BaseException as exc:  # pragma: no cover - test helper
                    errors.append(exc)

            def poller() -> None:
                try:
                    for _ in range(30):
                        poll_quota_logs(paths, rotate=False)
                        time.sleep(0.002)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer)]
            threads.extend(threading.Thread(target=poller) for _ in range(6))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            final = poll_quota_logs(paths, rotate=False)
            self.assertFalse(final.rotated)
            state = load_log_watch_state(paths.root)
            payload = json.loads(log_watch_state_path(paths.root).read_text(encoding="utf-8"))
            self.assertEqual(payload["cursors"][str(log_path)]["offset"], log_path.stat().st_size)
            self.assertEqual(state["cursors"][str(log_path)]["offset"], log_path.stat().st_size)

    def test_empty_init_then_first_quota_log_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "manager"
            live_dir = Path(tmp) / "live" / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            paths = build_paths(root)
            ensure_layout(paths)
            set_live_dir(paths, live_dir)
            add_account(paths, "account-a", _write_token_home(Path(tmp), "account-a"))
            add_account(paths, "account-b", _write_token_home(Path(tmp), "account-b"))
            set_switch_mode(paths, "auto")

            first = poll_quota_logs(paths, rotate=True)
            self.assertEqual(first.events, [])
            self.assertFalse(first.rotated)
            state = load_log_watch_state(paths.root)
            self.assertTrue(state["initialized"])
            self.assertEqual(state["cursors"], {})

            log_path = log_dir / "cli-new.log"
            log_path.write_text(SAMPLE_TUI_LINE + "\n", encoding="utf-8")
            second = poll_quota_logs(paths, rotate=True)
            self.assertEqual(len(second.events), 1)
            self.assertEqual(second.events[0].kind, "individual_quota")
            self.assertEqual(second.events[0].path, str(log_path))
            self.assertTrue(second.rotated)
            self.assertEqual(second.rotation.previous_active, "account-a")
            self.assertEqual(second.rotation.switched_to, "account-b")


if __name__ == "__main__":
    unittest.main()

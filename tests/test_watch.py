from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agy_cli_manager.watch import (
    consume_log_events,
    iter_live_agy_log_files,
    parse_quota_log_line,
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

    def test_new_file_during_watch_is_read_from_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / ".gemini"
            log_dir = live_dir / "antigravity-cli" / "log"
            log_dir.mkdir(parents=True)
            started_at = 1_000_000.0
            log_path = log_dir / "cli-new.log"
            log_path.write_text(SAMPLE_TUI_LINE + "\n", encoding="utf-8")
            os.utime(log_path, (started_at + 5, started_at + 5))
            _cursors, events = consume_log_events(
                live_dir,
                {},
                from_start=False,
                started_at=started_at,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "individual_quota")




class ResumeAgyArgsTests(unittest.TestCase):
    def test_adds_continue_when_missing(self) -> None:
        from agy_cli_manager.watch import resume_agy_args

        self.assertEqual(resume_agy_args([]), ["--continue"])
        self.assertEqual(resume_agy_args(["--prompt", "hi"]), ["--continue", "--prompt", "hi"])

    def test_keeps_continue_or_conversation(self) -> None:
        from agy_cli_manager.watch import resume_agy_args

        self.assertEqual(resume_agy_args(["--continue"]), ["--continue"])
        self.assertEqual(resume_agy_args(["-c", "--model", "x"]), ["-c", "--model", "x"])
        self.assertEqual(
            resume_agy_args(["--conversation", "abc"]),
            ["--conversation", "abc"],
        )
        self.assertEqual(
            resume_agy_args(["--conversation=abc"]),
            ["--conversation=abc"],
        )

    def test_strips_leading_double_dash(self) -> None:
        from agy_cli_manager.watch import resume_agy_args

        self.assertEqual(resume_agy_args(["--", "--model", "x"]), ["--continue", "--model", "x"])


class ClearRestartRequiredTests(unittest.TestCase):
    def test_clears_restart_flag(self) -> None:
        from agy_cli_manager.watch import clear_restart_required, load_log_watch_state, save_log_watch_state

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_log_watch_state(root, {"restart_required": True})
            clear_restart_required(root)
            self.assertFalse(load_log_watch_state(root)["restart_required"])

if __name__ == "__main__":
    unittest.main()

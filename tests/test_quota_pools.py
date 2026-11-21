"""Dynamic pool discovery is separate from the fixed family routing model."""
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agy_cli_manager import manager as m
from agy_cli_manager import cli
from agy_cli_manager.cli import _quota_pool_lines


USAGE = """Gemini Models\tWeekly Limit Remaining\t99%\t2026-11-27T06:14:42Z
Gemini Models\tFive Hour Limit Remaining\t99%\t2026-11-20T11:14:42Z
Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-11-27T06:29:08Z
Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-11-20T11:29:08Z
Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t100%\t2026-11-27T06:29:08Z
"""


class QuotaPoolTests(unittest.TestCase):
    def test_opening_and_scrolling_pool_view_does_not_query_agy(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (24, 80)
        screen.getch.side_effect = [cli.curses.KEY_DOWN, cli.curses.KEY_NPAGE, 27]
        payload = {"account": "a", "pools": m._parse_usage_pools(USAGE),
                   "refreshed_at": "2026-11-20T06:30:00Z", "source": "agy /usage"}
        with mock.patch.object(cli, "get_account_quota_pools", return_value=payload), \
             mock.patch.object(cli, "refresh_account_quota_pools") as refresh, \
             mock.patch.object(cli, "_color_attr", return_value=0):
            cli._quota_pools_dashboard(screen, None, "a")
        refresh.assert_not_called()
        painted = "\n".join(call.args[2] for call in screen.addstr.call_args_list)
        self.assertIn("Gemini 4 Argon", painted)
        self.assertIn("Quota Pools", painted)

    def test_discovers_three_pools_and_five_limits_without_merging_argon(self):
        pools = m._parse_usage_pools(USAGE)
        self.assertEqual(len(pools), 3)
        self.assertEqual(sum(map(len, pools.values())), 5)
        self.assertEqual(len(pools["Gemini 4 Argon"]), 1)
        self.assertEqual(pools["Gemini Models"][0]["remaining_percent"], 99)
        self.assertEqual(pools["Gemini 4 Argon"][0]["reset_at"], "2026-11-27T06:29:08+00:00")

    def test_future_pools_survive_and_invalid_rows_are_ignored(self):
        output = USAGE + "Future Pool\tDaily limit\t42.5%\t2026-11-21T00:00:00Z\n"
        output += "bad\tbad\t101%\t2026-11-21T00:00:00Z\n"
        output += "bad\tbad\t0%\tinvalid\n"
        pools = m._parse_usage_pools(output)
        self.assertIn("Future Pool", pools)
        self.assertNotIn("bad", pools)
        with self.assertRaises(ValueError):
            m._parse_usage_pools("Login required")

    def test_pool_screen_displays_week_only_pool_and_expired_cache(self):
        lines = _quota_pool_lines({"pools": m._parse_usage_pools(USAGE)},
                                  datetime(2026, 11, 21, tzinfo=timezone.utc))
        text = "\n".join(lines)
        self.assertIn("Gemini 4 Argon", text)
        self.assertEqual(text.count("% remaining"), 5)
        self.assertIn("due; refresh needed", text)

    def test_refresh_is_per_account_and_failure_preserves_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = m.build_paths(base / "manager")
            with mock.patch.object(m, "default_live_dir", return_value=base / "live/.gemini"):
                m.ensure_layout(paths)
            for name in ("a", "b"):
                source = base / name
                token = source / ".gemini/antigravity-cli/antigravity-oauth-token"
                token.parent.mkdir(parents=True)
                token.write_text("token-" + name)
                m.add_account(paths, name, source)
            live_token = base / "live/.gemini/antigravity-cli/antigravity-oauth-token"
            original_live = live_token.read_bytes()
            with mock.patch.object(m, "resolve_agy_binary", return_value="/fake/agy"), \
                 mock.patch.object(m.sys, "platform", "linux"), \
                 mock.patch.object(m.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=USAGE)) as run:
                payload = m.refresh_account_quota_pools(paths, "b")
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["env"]["HOME"], str(m.account_dir(paths, "b")))
            self.assertEqual(kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"],
                             "unix:path=/tmp/agy-cli-manager-isolated-no-keyring")
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(len(payload["pools"]), 3)
            self.assertEqual(m.load_state(paths)["active"], "a")
            self.assertEqual(live_token.read_bytes(), original_live)
            with mock.patch.object(m.subprocess, "run") as run:
                self.assertEqual(m.get_account_quota_pools(paths, "b"), payload)
                run.assert_not_called()
            for outcome in (mock.Mock(returncode=1), mock.Mock(returncode=0, stdout="bad output"),
                            subprocess.TimeoutExpired("agy", 30)):
                patch = (mock.patch.object(m.subprocess, "run", side_effect=outcome)
                         if isinstance(outcome, Exception)
                         else mock.patch.object(m.subprocess, "run", return_value=outcome))
                with mock.patch.object(m, "resolve_agy_binary", return_value="/fake/agy"), patch:
                    with self.assertRaises(ValueError):
                        m.refresh_account_quota_pools(paths, "b")
                self.assertEqual(m.get_account_quota_pools(paths, "b"), payload)


if __name__ == "__main__":
    unittest.main()

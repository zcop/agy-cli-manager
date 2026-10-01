"""Regression coverage for manager state and account lifecycle bugs."""

from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agy_cli_manager import manager as m


TOKEN_PATH = Path(".gemini/antigravity-cli/antigravity-oauth-token")


class ManagerRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-manager-test-")
        self.addCleanup(self.tmp.cleanup)
        # Production code resolves account paths; on macOS tempfile lives
        # under /var, a symlink to /private/var, so resolve to match.
        self.base = Path(self.tmp.name).resolve()
        self.live_home = self.base / "live"
        self.paths = m.build_paths(self.base / "manager")
        live_patch = mock.patch.object(m, "default_live_dir", return_value=self.live_home / ".gemini")
        live_patch.start()
        self.addCleanup(live_patch.stop)
        m.ensure_layout(self.paths)

    @staticmethod
    def token(home: Path) -> Path:
        return home / TOKEN_PATH

    def add(self, name: str) -> None:
        source = self.base / f"source-{name}"
        token = self.token(source)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(f"token-{name}", encoding="utf-8")
        m.add_account(self.paths, name, source)

    def test_account_paths_cannot_escape_or_follow_symlink(self) -> None:
        self.add("a")
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        for name in ("../../outside", "../outside", ".", "..", "bad\\name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                m.save_account_profile(self.paths, name, self.base / "source-a", overwrite=True)
        (self.paths.accounts_dir / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            m.save_account_profile(self.paths, "linked", self.base / "source-a", overwrite=True)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_interrupted_state_write_keeps_previous_state(self) -> None:
        self.add("a")
        old_state = self.paths.state_file.read_text(encoding="utf-8")

        def interrupted_dump(data, handle, **kwargs):
            handle.write('{"active":')
            raise OSError("simulated interrupted write")

        with mock.patch.object(m.json, "dump", side_effect=interrupted_dump):
            with self.assertRaises(OSError):
                m.save_state(self.paths, m.load_state(self.paths))
        self.assertEqual(self.paths.state_file.read_text(encoding="utf-8"), old_state)

    def test_status_read_does_not_revert_concurrent_switch(self) -> None:
        self.add("a")
        self.add("b")
        original_sync = m.sync_state_from_disk
        switched = False

        def interleaved_switch(paths, state):
            nonlocal switched
            result = original_sync(paths, state)
            if not switched:
                switched = True
                m.switch_account(paths, "b")
            return result

        with mock.patch.object(m, "sync_state_from_disk", side_effect=interleaved_switch):
            m.format_status(self.paths)
        self.assertEqual(m.load_state(self.paths)["active"], "b")

    def test_clearing_live_dir_stays_cleared_after_reload_and_switch(self) -> None:
        self.add("a")
        self.add("b")
        original_live = self.token(self.live_home).read_text(encoding="utf-8")
        m.set_live_dir(self.paths, None)
        self.assertIsNone(m.get_live_dir(m.load_state(self.paths)))
        m.switch_account(self.paths, "b")
        self.assertIsNone(m.get_live_dir(m.load_state(self.paths)))
        self.assertEqual(self.token(self.live_home).read_text(encoding="utf-8"), original_live)

    def test_failover_with_only_invalid_standbys_finishes_cleanly(self) -> None:
        self.add("a")
        self.add("b")
        self.token(m.account_dir(self.paths, "b")).unlink()
        result = m.rotate_after_failure(self.paths, "quota")
        state = m.load_state(self.paths)
        self.assertEqual(result.outcome, "no_candidate")
        self.assertIsNone(state["active"])
        self.assertEqual(state["switch_runtime"]["status"], "no_account")
        self.assertEqual(state["accounts"]["a"]["fail_count"], 1)

    def test_named_models_probe_does_not_touch_live_or_runtime_during_switch(self) -> None:
        for name in ("a", "b", "c"):
            self.add(name)

        def run_models(home, **kwargs):
            self.assertEqual(home, m.account_dir(self.paths, "b"))
            m.switch_account(self.paths, "c")
            return [{"name": "model"}]

        with mock.patch.object(m, "_run_agy_models_command", side_effect=run_models):
            m.list_models(self.paths, "b")
        self.assertEqual(m.load_state(self.paths)["active"], "c")
        self.assertEqual(self.token(self.live_home).read_text(encoding="utf-8"), "token-c")
        self.assertEqual(self.token(self.paths.runtime_dir).read_text(encoding="utf-8"), "token-c")

    def test_identity_probe_uses_selected_account_home(self) -> None:
        self.add("a")
        self.add("b")

        def run_probe(*args, **kwargs):
            self.assertEqual(kwargs["env"]["HOME"], str(m.account_dir(self.paths, "b")))
            m.switch_account(self.paths, "b")
            return subprocess.CompletedProcess(args[0], 0, "b@example.com", "")

        with mock.patch.object(m, "resolve_agy_binary", return_value="agy"), \
             mock.patch.object(m.subprocess, "run", side_effect=run_probe):
            identity = m.probe_profile_identity_via_usage(m.account_dir(self.paths, "b"))
        self.assertEqual(identity["account_name"], "b@example.com")
        self.assertEqual(self.token(self.live_home).read_text(encoding="utf-8"), "token-b")

    def test_active_usage_refresh_does_not_copy_new_active_token_to_old_account(self) -> None:
        self.add("a")
        self.add("b")
        payload = json.dumps({"token": {"access_token": "access-a"}})
        self.token(self.live_home).write_text(payload, encoding="utf-8")
        self.token(m.account_dir(self.paths, "a")).write_text(payload, encoding="utf-8")

        def fake_cloudcode(token, endpoint, body):
            self.assertEqual(token, "access-a")
            if endpoint == m.CODE_ASSIST_LOAD_PATH:
                m.switch_account(self.paths, "b")
                return {"cloudaicompanionProject": "project-a"}
            return {"groups": []}

        with mock.patch.object(m, "_cloudcode_request", side_effect=fake_cloudcode), \
             mock.patch.object(m, "_best_effort_live_identity", return_value=None):
            result = m.refresh_account_usage(self.paths)
        self.assertEqual(result.account, "a")
        self.assertEqual(self.token(m.account_dir(self.paths, "a")).read_text(encoding="utf-8"), payload)
        self.assertEqual(self.token(self.live_home).read_text(encoding="utf-8"), "token-b")

    def test_failed_login_preserves_live_token(self) -> None:
        self.add("a")
        with mock.patch.object(m.os, "isatty", return_value=True), \
             mock.patch.object(m, "resolve_agy_binary", return_value="/fake/agy"), \
             mock.patch.object(m.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaises(ValueError):
                m.login_account(self.paths, "b", None)
        self.assertEqual(self.token(self.live_home).read_text(encoding="utf-8"), "token-a")
        self.assertEqual(m.load_state(self.paths)["active"], "a")

    def test_successful_standby_login_keeps_current_live_account(self) -> None:
        self.add("a")

        def fake_login(*args, **kwargs):
            home = Path(kwargs["env"]["HOME"])
            self.assertNotEqual(home, self.live_home)
            token = self.token(home)
            token.parent.mkdir(parents=True)
            token.write_text("token-b", encoding="utf-8")
            return mock.Mock(poll=mock.Mock(return_value=0))

        with mock.patch.object(m.os, "isatty", return_value=True), \
             mock.patch.object(m, "resolve_agy_binary", return_value="/fake/agy"), \
             mock.patch.object(m.subprocess, "Popen", side_effect=fake_login), \
             mock.patch.object(m, "resolve_login_profile_identity", return_value={}), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(m.login_account(self.paths, "b", None), "b")
        self.assertEqual(m.load_state(self.paths)["active"], "a")
        self.assertEqual(self.token(self.live_home).read_text(encoding="utf-8"), "token-a")
        self.assertEqual(self.token(m.account_dir(self.paths, "b")).read_text(encoding="utf-8"), "token-b")

    def test_macos_keychain_is_linked_into_spawned_agy_homes(self) -> None:
        self.add("a")
        real_keychains = self.base / "Library" / "Keychains"
        real_keychains.mkdir(parents=True)
        account_home = m.account_dir(self.paths, "a")
        with mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m.Path, "home", return_value=self.base), \
             mock.patch.object(m, "resolve_agy_binary", return_value="agy"), \
             mock.patch.object(
                 m.subprocess,
                 "run",
                 return_value=subprocess.CompletedProcess(["agy"], 0, "Gemini 3 Pro", ""),
             ):
            models = m._run_agy_models_command(account_home)
        self.assertEqual(models[0]["name"], "Gemini 3 Pro")
        link = account_home / "Library" / "Keychains"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), real_keychains.resolve())

    def test_login_home_links_macos_keychain(self) -> None:
        self.add("a")
        real_keychains = self.base / "Library" / "Keychains"
        real_keychains.mkdir(parents=True)
        observed: dict = {}

        def fake_login(*args, **kwargs):
            home = Path(kwargs["env"]["HOME"])
            link = home / "Library" / "Keychains"
            observed["linked"] = link.is_symlink() and link.resolve() == real_keychains.resolve()
            token = self.token(home)
            token.parent.mkdir(parents=True)
            token.write_text("token-b", encoding="utf-8")
            return mock.Mock(poll=mock.Mock(return_value=0))

        with mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m.Path, "home", return_value=self.base), \
             mock.patch.object(m.os, "isatty", return_value=True), \
             mock.patch.object(m, "resolve_agy_binary", return_value="/fake/agy"), \
             mock.patch.object(m.subprocess, "Popen", side_effect=fake_login), \
             mock.patch.object(m, "resolve_login_profile_identity", return_value={}), \
             redirect_stdout(io.StringIO()):
            m.login_account(self.paths, "b", None)
        self.assertTrue(observed["linked"])

    def test_quota_summary_preserves_gemini_and_other_families(self) -> None:
        summary = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.5, "resetTime": "2026-09-27T19:20:24Z"},
                        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.25, "resetTime": "2026-09-29T06:06:04Z"},
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
                    "buckets": [
                        {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.75, "resetTime": "2026-09-27T19:20:24Z"},
                        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1.0, "resetTime": "2026-10-04T14:20:24Z"},
                    ],
                },
            ]
        }
        families, bucket_count = m._parse_quota_families_from_summary(summary)
        self.assertEqual(bucket_count, 4)
        self.assertEqual(families["gemini"]["short"]["value"], 50.0)
        self.assertEqual(families["gemini"]["weekly"]["value"], 25.0)
        self.assertEqual(families["other"]["short"]["value"], 75.0)
        self.assertEqual(families["other"]["weekly"]["value"], 100.0)

    def test_legacy_usage_windows_migrate_to_gemini_family(self) -> None:
        meta = {
            "usage_windows": {
                "short": {"status": "known", "value": 42.0, "reset_at": None},
                "weekly": {"status": "known", "value": 84.0, "reset_at": None},
            }
        }
        m._sync_legacy_usage_fields(meta)
        self.assertEqual(meta["usage_families"]["gemini"], meta["usage_windows"])
        self.assertEqual(meta["usage_families"]["other"]["short"]["status"], "unknown")

    def test_family_aware_failover_uses_requested_family_quota(self) -> None:
        self.add("a")
        self.add("b")
        state = m.load_state(self.paths)
        for name, gemini_value, other_value in (
            ("a", 90.0, 0.0),
            ("b", 0.0, 80.0),
        ):
            state["accounts"][name]["usage_families"] = {
                "gemini": {
                    "short": {"status": "known", "value": gemini_value, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": 90.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                },
                "other": {
                    "short": {"status": "known", "value": other_value, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": 90.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                },
            }
            m._sync_legacy_usage_fields(state["accounts"][name])
        m.save_state(self.paths, state)
        m.switch_account(self.paths, "a")

        result = m.ensure_active_account(self.paths, required_family="other")

        self.assertTrue(result.triggered)
        self.assertEqual(result.reason, "other_quota_exhausted")
        self.assertEqual(result.switched_to, "b")
        updated = m.load_state(self.paths)
        self.assertEqual(updated["active"], "b")
        self.assertIsNone(updated["accounts"]["a"].get("cooldown_until"))
        self.assertIsNotNone(updated["accounts"]["a"]["family_cooldowns"]["other"])

    def test_family_aware_failover_does_not_select_depleted_candidate(self) -> None:
        self.add("a")
        self.add("b")
        state = m.load_state(self.paths)
        for name in ("a", "b"):
            state["accounts"][name]["usage_families"] = {
                family: {
                    "short": {"status": "known", "value": 0.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": 50.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                }
                for family in ("gemini", "other")
            }
            m._sync_legacy_usage_fields(state["accounts"][name])
        m.save_state(self.paths, state)
        m.switch_account(self.paths, "a")

        result = m.ensure_active_account(self.paths, required_family="gemini")

        self.assertIsNone(result.switched_to)
        self.assertIsNone(m.load_state(self.paths)["active"])

    def test_switch_policy_supports_per_family_thresholds(self) -> None:
        policy = m.update_switch_policy(
            self.paths,
            gemini_usage_threshold_percent=15.0,
            other_usage_threshold_percent=5.0,
        )
        self.assertEqual(policy["family_thresholds"], {"gemini": 15.0, "other": 5.0})

    def test_route_defaults_to_same_family_on_another_account(self) -> None:
        self.add("a")
        self.add("b")
        state = m.load_state(self.paths)
        for name, gemini_value in (("a", 0.0), ("b", 80.0)):
            state["accounts"][name]["usage_families"] = {
                "gemini": {
                    "short": {"status": "known", "value": gemini_value, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": 80.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                },
                "other": {
                    "short": {"status": "known", "value": 90.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": 90.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                },
            }
            m._sync_legacy_usage_fields(state["accounts"][name])
        m.save_state(self.paths, state)
        m.switch_account(self.paths, "a")

        result = m.resolve_route(self.paths, "gemini")

        self.assertEqual(result.outcome, "account_switch")
        self.assertEqual(result.selected_family, "gemini")
        self.assertEqual(result.active, "b")

    def test_route_can_prefer_other_family_on_same_account(self) -> None:
        self.add("a")
        self.add("b")
        state = m.load_state(self.paths)
        for name, gemini_value in (("a", 0.0), ("b", 80.0)):
            state["accounts"][name]["usage_families"] = {
                "gemini": {
                    "short": {"status": "known", "value": gemini_value, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": 80.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                },
                "other": {
                    "short": {"status": "known", "value": 90.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": 90.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                },
            }
            m._sync_legacy_usage_fields(state["accounts"][name])
        m.save_state(self.paths, state)
        m.switch_account(self.paths, "a")

        result = m.resolve_route(self.paths, "gemini", fallback_strategy="same-account-first")

        self.assertEqual(result.outcome, "family_fallback")
        self.assertEqual(result.selected_family, "other")
        self.assertEqual(result.active, "a")
        self.assertIsNone(result.switched_to)

    def test_weekly_exhaustion_triggers_family_failover(self) -> None:
        self.add("a")
        self.add("b")
        state = m.load_state(self.paths)
        for name, weekly_value in (("a", 0.0), ("b", 75.0)):
            state["accounts"][name]["usage_families"] = {
                "gemini": {
                    "short": {"status": "known", "value": 100.0, "reset_at": "2099-01-01T00:00:00+00:00"},
                    "weekly": {"status": "known", "value": weekly_value, "reset_at": "2099-01-02T00:00:00+00:00"},
                },
                "other": m._default_usage_windows(),
            }
            m._sync_legacy_usage_fields(state["accounts"][name])
        m.save_state(self.paths, state)
        m.switch_account(self.paths, "a")

        result = m.ensure_active_account(self.paths, required_family="gemini")

        self.assertEqual(result.reason, "gemini_quota_exhausted")
        self.assertEqual(result.switched_to, "b")

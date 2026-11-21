import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

from agy_cli_manager import manager as m, cli
from agy_cli_manager.quota_policy import evaluate
from agy_cli_manager.watch import poll_quota_logs


class QuotaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.policy = {"configured": True, "enabled": True, "pools": ["Gemini", "Argon"],
                       "match": "any", "threshold_percent": 10, "max_age_seconds": 1800}

    def meta(self, gemini=99, argon=99):
        reset = (self.now + timedelta(hours=2)).isoformat()
        return {"quota_pools_refreshed_at": self.now.isoformat(), "quota_pools": {
            "Gemini": [{"label": "5h", "remaining_percent": gemini, "reset_at": reset}],
            "Argon": [{"label": "Weekly", "remaining_percent": argon, "reset_at": reset}]}}

    def test_any_all_and_single_pool_truth_table(self):
        for match, values, trigger, usable in (
            ("any", (0, 99), True, False), ("any", (99, 99), False, True),
            ("all", (0, 99), False, True), ("all", (0, 0), True, False),
        ):
            with self.subTest(match=match, values=values):
                result = evaluate(self.meta(*values), {**self.policy, "match": match}, self.now)
                self.assertEqual(result["triggered"], trigger)
                self.assertEqual(result["replacement_usable"], usable)
        policy = {**self.policy, "pools": ["Argon"]}
        self.assertFalse(evaluate(self.meta(0, 99), policy, self.now)["triggered"])
        self.assertTrue(evaluate(self.meta(99, 10), policy, self.now)["triggered"])

    def test_weekly_limit_can_exhaust_pool_and_absent_pool_is_unknown(self):
        meta = self.meta()
        meta['quota_pools']['Gemini'].append({**meta['quota_pools']['Gemini'][0], 'label': 'Weekly', 'remaining_percent': 0})
        self.assertTrue(evaluate(meta, self.policy, self.now)['triggered'])
        del meta['quota_pools']['Argon']
        self.assertFalse(evaluate(meta, {**self.policy, 'match': 'all'}, self.now)['triggered'])
        self.assertFalse(evaluate(meta, self.policy, self.now)['replacement_usable'])

    def test_stale_future_and_expired_data_are_not_available_or_exhausted(self):
        for age in (timedelta(hours=1), timedelta(seconds=-1)):
            meta = self.meta(0, 0)
            meta['quota_pools_refreshed_at'] = (self.now - age).isoformat()
            result = evaluate(meta, self.policy, self.now)
            self.assertFalse(result['triggered'])
            self.assertFalse(result['replacement_usable'])
        meta = self.meta(0, 0)
        for limits in meta['quota_pools'].values():
            limits[0]['reset_at'] = (self.now - timedelta(seconds=1)).isoformat()
        result = evaluate(meta, self.policy, self.now)
        self.assertFalse(result['triggered'])
        self.assertFalse(result['replacement_usable'])

    def test_disabled_and_empty_selection_never_trigger(self):
        for extra in ({'enabled': False}, {'pools': []}):
            self.assertFalse(evaluate(self.meta(0, 0), {**self.policy, **extra}, self.now)['triggered'])

    def setup_manager(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        paths = m.build_paths(base / 'manager')
        with mock.patch.object(m, 'default_live_dir', return_value=base / 'live/.gemini'):
            m.ensure_layout(paths)
        for name in ('a', 'b', 'c'):
            token = base / name / '.gemini/antigravity-cli/antigravity-oauth-token'
            token.parent.mkdir(parents=True)
            token.write_text('token-' + name)
            m.add_account(paths, name, base / name)
        m.update_quota_policy(paths, **{k: v for k, v in self.policy.items() if k != 'configured'})
        return paths

    def set_quotas(self, paths, quotas):
        state = m.load_state(paths)
        for name, values in quotas.items():
            state['accounts'][name].update(self.meta(*values))
        m.save_state(paths, state)

    def test_any_skips_depleted_standby_and_switches_to_fully_available(self):
        paths = self.setup_manager()
        self.set_quotas(paths, {'a': (0, 99), 'b': (99, 0), 'c': (99, 99)})
        result = m.ensure_active_account(paths)
        self.assertEqual(result.switched_to, 'c')
        self.assertEqual(m.load_state(paths)['active'], 'c')
        self.assertIsNone(m.load_state(paths)['accounts']['a']['cooldown_until'])
        self.assertEqual(m.get_status_snapshot(paths)['accounts']['c']['quota_evaluation']['pools']['Argon'], 'available')

    def test_all_switches_only_when_every_selected_pool_exhausted(self):
        paths = self.setup_manager()
        m.update_quota_policy(paths, match='all')
        self.set_quotas(paths, {'a': (0, 99), 'b': (99, 0)})
        self.assertIsNone(m.ensure_active_account(paths).switched_to)
        self.set_quotas(paths, {'a': (0, 0)})
        self.assertEqual(m.ensure_active_account(paths).switched_to, 'b')

    def test_unknown_standby_keeps_current_account_and_profile(self):
        paths = self.setup_manager()
        self.set_quotas(paths, {'a': (0, 0)})
        before = (paths.runtime_dir / '.gemini/antigravity-cli/antigravity-oauth-token').read_bytes()
        result = m.ensure_active_account(paths)
        self.assertIsNone(result.switched_to)
        self.assertEqual(result.reason, 'pool_quota_exhausted')
        self.assertEqual(m.load_state(paths)['active'], 'a')
        self.assertEqual((paths.runtime_dir / '.gemini/antigravity-cli/antigravity-oauth-token').read_bytes(), before)

    def test_manual_mode_and_disabled_quota_preserve_active(self):
        paths = self.setup_manager()
        self.set_quotas(paths, {'a': (0, 0), 'b': (99, 99)})
        m.set_switch_mode(paths, 'manual')
        self.assertIsNone(m.ensure_active_account(paths).switched_to)
        m.set_switch_mode(paths, 'auto')
        m.update_quota_policy(paths, enabled=False)
        self.assertIsNone(m.ensure_active_account(paths).switched_to)
        state = m.load_state(paths)
        state['accounts']['a']['refresh_fail_count'] = 2
        m.save_state(paths, state)
        self.assertIsNotNone(m.ensure_active_account(paths).switched_to)

    def test_invalid_policy_does_not_change_saved_state(self):
        paths = self.setup_manager()
        before = paths.state_file.read_bytes()
        for changes in ({'pools': []}, {'match': 'invalid'}, {'threshold_percent': float('nan')},
                        {'max_age_seconds': 0}, {'pools': ['']}, {'pools': 'Argon'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                m.update_quota_policy(paths, **changes)
            self.assertEqual(paths.state_file.read_bytes(), before)

    def test_failed_activation_preserves_active_account_and_quota_metadata(self):
        paths = self.setup_manager()
        self.set_quotas(paths, {'a': (0, 0), 'b': (99, 99)})
        before = paths.state_file.read_bytes()
        with mock.patch.object(m, '_activate_account_locked', side_effect=ValueError('keyring unavailable')):
            with self.assertRaises(ValueError):
                m.ensure_active_account(paths)
        self.assertEqual(paths.state_file.read_bytes(), before)

    def test_log_watch_cannot_bypass_all_policy_or_disabled_toggle(self):
        paths = self.setup_manager()
        self.set_quotas(paths, {'a': (0, 99), 'b': (99, 99)})
        log = m.get_live_dir(m.load_state(paths)) / 'antigravity-cli/log/cli-test.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('Individual quota reached\n')
        for enabled, values, should_switch in ((True, (0, 99), False),
                                                (False, (0, 0), False),
                                                (True, (0, 0), True)):
            m.update_quota_policy(paths, enabled=enabled, match='all')
            self.set_quotas(paths, {'a': values})
            result = poll_quota_logs(paths, from_start=True)
            self.assertEqual(result.rotated, should_switch)
        self.assertEqual(m.load_state(paths)['active'], 'b')

    def test_settings_cancel_and_save_are_explicit(self):
        paths = self.setup_manager()
        self.set_quotas(paths, {'a': (99, 99)})
        screen = mock.Mock()
        screen.getmaxyx.return_value = (24, 100)
        for keys, saved in (([ord('c'), 27], False), ([ord('c'), ord(' '), ord('m'), ord('s')], True)):
            before = m.get_quota_policy(paths)
            screen.getch.side_effect = keys
            with mock.patch.object(cli, '_color_attr', return_value=0):
                cli._quota_policy_dashboard(screen, paths)
            after = m.get_quota_policy(paths)
            if saved:
                self.assertEqual(after['pools'], ['Argon'])
                self.assertEqual(after['match'], 'all')
            else:
                self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()

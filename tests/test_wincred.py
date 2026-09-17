"""Isolated Windows Credential Manager tests.

HARD RULE: CredWriteW/CredDeleteW only against targets that start with
``agy-cli-manager-test:``. Tests always pass
``dest_target="agy-cli-manager-test:live"`` into ``apply_to_live`` so they
never touch ``gemini:antigravity``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

TEST_PREFIX = "agy-cli-manager-test:"
TEST_LIVE = f"{TEST_PREFIX}live"
REAL_LIVE = "gemini:antigravity"


def _assert_test_target(target: str, op: str) -> None:
    name = str(target or "")
    if not name.startswith(TEST_PREFIX):
        raise AssertionError(f"ABORT: {op} target {name!r} does not start with {TEST_PREFIX!r}")
    if name == REAL_LIVE or name.startswith("gemini:"):
        raise AssertionError(f"ABORT: {op} attempted on {name!r}")


@unittest.skipUnless(os.name == "nt", "Windows Credential Manager tests require Windows")
class WindowsCredentialSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from agy_cli_manager import credential_store as cs

        cls.cs = cs
        cls._orig_write = cs._cred_write
        cls._orig_delete = cs._cred_delete

        def guarded_write(target: str, blob: bytes, persist: int, username: str) -> None:
            _assert_test_target(target, "CredWriteW")
            cls._orig_write(target, blob, persist, username)

        def guarded_delete(target: str) -> bool:
            _assert_test_target(target, "CredDeleteW")
            return cls._orig_delete(target)

        cs._cred_write = guarded_write
        cs._cred_delete = guarded_delete

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cs._cred_write = cls._orig_write
        cls.cs._cred_delete = cls._orig_delete

    def setUp(self) -> None:
        self._prev_live_env = os.environ.get("AGY_WINCRED_TARGET")
        os.environ["AGY_WINCRED_TARGET"] = TEST_LIVE
        self._cleanup_test_targets()

    def tearDown(self) -> None:
        self._cleanup_test_targets()
        if self._prev_live_env is None:
            os.environ.pop("AGY_WINCRED_TARGET", None)
        else:
            os.environ["AGY_WINCRED_TARGET"] = self._prev_live_env

    def _cleanup_test_targets(self) -> None:
        for name in self.cs.enumerate_targets(f"{TEST_PREFIX}*"):
            if name.startswith(TEST_PREFIX):
                self.cs.WindowsCredentialStore(name).delete()

    def _store(self, suffix: str):
        target = f"{TEST_PREFIX}{suffix}"
        _assert_test_target(target, "store")
        return self.cs.WindowsCredentialStore(target)

    def _apply_to_test_live(self, source_target: str) -> None:
        self.cs.apply_to_live(source_target, dest_target=TEST_LIVE)

    def test_read_write_delete_exists(self) -> None:
        store = self._store(f"crud-{uuid.uuid4().hex[:8]}")
        self.assertFalse(store.exists())
        self.assertFalse(store.delete())
        with self.assertRaises(OSError) as ctx:
            store.read()
        self.assertEqual(ctx.exception.winerror, 1168)

        payload = b"agy-cli-manager-test-blob-v1"
        store.write(payload)
        self.assertTrue(store.exists())
        self.assertEqual(store.read(), payload)
        self.assertTrue(store.delete())
        self.assertFalse(store.exists())
        with self.assertRaises(OSError) as ctx:
            store.read()
        self.assertEqual(ctx.exception.winerror, 1168)

    def test_blob_size_limit(self) -> None:
        store = self._store("too-big")
        self.assertEqual(self.cs.CRED_BLOB_MAX, 2560)
        dll = self.cs._advapi32()
        orig_cred_write = dll.CredWriteW
        calls: list[int] = []

        def spy_cred_write(cred, flags):
            calls.append(int(flags or 0))
            return orig_cred_write(cred, flags)

        dll.CredWriteW = spy_cred_write
        try:
            with self.assertRaises(self.cs.CredentialStoreError) as ctx:
                store.write(b"X" * 2561)
            self.assertIn("2560", str(ctx.exception))
            self.assertEqual(calls, [])
            self.assertFalse(store.exists())
        finally:
            dll.CredWriteW = orig_cred_write

    def test_copy_slot_and_apply_to_live(self) -> None:
        one = self._store("p-one")
        two = self._store("p-two")
        copied = self._store("p-copy")
        blob_one = b"FAKE-BLOB-ONE-" + b"A" * 48
        blob_two = b"FAKE-BLOB-TWO-" + b"B" * 48
        one.write(blob_one)
        two.write(blob_two)

        self._apply_to_test_live(one.target_name)
        live = self.cs.WindowsCredentialStore(TEST_LIVE)
        self.assertEqual(live.read(), blob_one)

        self._apply_to_test_live(two.target_name)
        self.assertEqual(live.read(), blob_two)

        self._apply_to_test_live(one.target_name)
        self.assertEqual(live.read(), blob_one)

        self.cs.copy_slot(one.target_name, copied.target_name)
        self.assertEqual(copied.read(), blob_one)

    def test_legacy_json_migration(self) -> None:
        name = f"mig-{uuid.uuid4().hex[:8]}"
        payload = {
            "token": {
                "access_token": "ya29.fake-test-token",
                "refresh_token": "1//fake-refresh",
            }
        }
        blob = json.dumps(payload).encode("utf-8")

        def test_profile_target(account: str) -> str:
            return f"{TEST_PREFIX}{account}"

        with tempfile.TemporaryDirectory(prefix="agy-wincred-mig-") as tmp:
            account_dir = Path(tmp) / name
            account_dir.mkdir()
            json_path = account_dir / "wincred_antigravity.json"
            json_path.write_bytes(blob)
            with mock.patch.object(self.cs, "profile_target", test_profile_target):
                migrated = self.cs.migrate_legacy_json(account_dir, name)
            self.assertTrue(migrated)
            self.assertFalse(json_path.exists())
            meta_path = account_dir / "credential.meta.json"
            self.assertTrue(meta_path.is_file())
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            store = self._store(name)
            self.assertTrue(store.exists())
            self.assertEqual(store.read(), blob)
            self.assertEqual(meta.get("TargetName"), f"{TEST_PREFIX}{name}")
            self.assertEqual(meta.get("blob_length"), len(blob))
            self.assertEqual(meta.get("profile_name"), name)

    def test_live_process_guard(self) -> None:
        fake_procs = ["agy.exe pid=1", "Antigravity.exe pid=2"]
        with mock.patch.object(
            self.cs, "list_conflicting_live_processes", return_value=fake_procs
        ):
            with self.assertRaises(self.cs.CredentialStoreError) as ctx:
                self.cs.assert_live_slot_idle(REAL_LIVE)
            message = str(ctx.exception)
            self.assertIn("agy.exe", message)
            self.assertIn("--force", message)

            self.cs.assert_live_slot_idle(REAL_LIVE, force=True)
            self.cs.assert_live_slot_idle(TEST_LIVE)

            source = self._store("guard-src")
            source.write(b"FAKE-GUARD-BLOB")
            self._apply_to_test_live(source.target_name)
            live = self.cs.WindowsCredentialStore(TEST_LIVE)
            self.assertEqual(live.read(), b"FAKE-GUARD-BLOB")


if __name__ == "__main__":
    unittest.main()

"""Linux Secret Service backend tests without touching a real wallet."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from agy_cli_manager import credential_store as cs


def credential(label: str) -> bytes:
    return json.dumps({"token": {"access_token": f"access-{label}", "refresh_token": f"refresh-{label}"}}).encode()


class LinuxSecretServiceTests(unittest.TestCase):
    def test_lookup_uses_exact_two_attribute_identity(self) -> None:
        payload = credential("one")
        completed = subprocess.CompletedProcess([], 0, payload, b"")
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/fake"}), \
             mock.patch.object(cs, "_secret_tool_binary", return_value="secret-tool"), \
             mock.patch.object(cs.subprocess, "run", return_value=completed) as run:
            self.assertEqual(cs.read_linux_live_credential(required=True), payload)
        command = run.call_args.args[0]
        self.assertEqual(command, ["secret-tool", "lookup", "service", "gemini", "username", "antigravity"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_store_verifies_exact_round_trip_without_shell(self) -> None:
        payload = credential("two")
        results = [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, payload, b""),
        ]
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/fake"}), \
             mock.patch.object(cs, "_secret_tool_binary", return_value="secret-tool"), \
             mock.patch.object(cs.subprocess, "run", side_effect=results) as run:
            cs.write_linux_live_credential(payload)
        self.assertEqual(run.call_args_list[0].kwargs["input"], payload)
        self.assertNotIn("shell", run.call_args_list[0].kwargs)

    def test_invalid_credential_is_rejected_before_store(self) -> None:
        with mock.patch.object(cs.subprocess, "run") as run, self.assertRaises(cs.CredentialStoreError):
            cs.write_linux_live_credential(b"not-json")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

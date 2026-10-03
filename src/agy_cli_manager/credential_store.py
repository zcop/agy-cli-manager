"""Platform credential-store helpers.

Linux Antigravity CLI uses the Secret Service item identified by
``service=gemini`` and ``username=antigravity``. Saved manager profiles stay
as private files; this module only bridges the single live Secret Service
slot without ever rendering credential bytes in diagnostics.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


class CredentialStoreError(ValueError):
    """Raised when a platform credential operation cannot complete safely."""


SECRET_TOOL_ENV = "AGY_MANAGER_SECRET_TOOL"
SECRET_SERVICE_ATTRIBUTES = ("service", "gemini", "username", "antigravity")
SECRET_SERVICE_LABEL = "Password for 'antigravity' on 'gemini'"
DEFAULT_TIMEOUT_SECONDS = 15


def validate_antigravity_credential(blob: bytes) -> bytes:
    payload = bytes(blob).strip()
    if not payload:
        raise CredentialStoreError("Antigravity credential is empty.")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialStoreError("Antigravity credential is not valid UTF-8 JSON.") from exc
    token = decoded.get("token") if isinstance(decoded, dict) else None
    if not isinstance(token, dict) or not isinstance(token.get("access_token"), str):
        raise CredentialStoreError("Antigravity credential is missing token.access_token.")
    return payload


def _secret_tool_binary() -> str | None:
    override = os.getenv(SECRET_TOOL_ENV, "").strip()
    if override:
        return override
    return shutil.which("secret-tool")


def linux_secret_service_available() -> bool:
    if not sys.platform.startswith("linux") or not os.getenv("DBUS_SESSION_BUS_ADDRESS", "").strip():
        return False
    binary = _secret_tool_binary()
    if binary is None:
        return False
    try:
        probe = subprocess.run(
            [binary, "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # libsecret's secret-tool prints its usage and exits 2 for --help.
    return probe.returncode in {0, 2}


def _require_secret_tool() -> str:
    if not sys.platform.startswith("linux"):
        raise CredentialStoreError("Linux Secret Service is only available on Linux.")
    if not os.getenv("DBUS_SESSION_BUS_ADDRESS", "").strip():
        raise CredentialStoreError("Linux Secret Service requires a D-Bus session.")
    binary = _secret_tool_binary()
    if binary is None:
        raise CredentialStoreError(
            "Linux Secret Service credential detected, but `secret-tool` is unavailable. "
            "Install the libsecret command-line tools or select the file credential backend."
        )
    return binary


def _run_secret_tool(
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    binary = _require_secret_tool()
    try:
        return subprocess.run(
            [binary, *arguments],
            input=input_bytes,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CredentialStoreError("Linux Secret Service operation timed out.") from exc
    except OSError as exc:
        raise CredentialStoreError("Unable to execute the Linux Secret Service helper.") from exc


def read_linux_live_credential(*, required: bool = False) -> bytes | None:
    result = _run_secret_tool(["lookup", *SECRET_SERVICE_ATTRIBUTES])
    if result.returncode != 0:
        if required:
            raise CredentialStoreError("No Antigravity credential was found in Linux Secret Service.")
        return None
    return validate_antigravity_credential(result.stdout)


def write_linux_live_credential(blob: bytes) -> None:
    payload = validate_antigravity_credential(blob)
    result = _run_secret_tool(
        ["store", f"--label={SECRET_SERVICE_LABEL}", *SECRET_SERVICE_ATTRIBUTES],
        input_bytes=payload,
    )
    if result.returncode != 0:
        raise CredentialStoreError("Unable to store the Antigravity credential in Linux Secret Service.")
    stored = read_linux_live_credential(required=True)
    if stored != payload:
        raise CredentialStoreError("Linux Secret Service credential verification failed.")


def clear_linux_live_credential() -> bool:
    existing = read_linux_live_credential(required=False)
    if existing is None:
        return False
    result = _run_secret_tool(["clear", *SECRET_SERVICE_ATTRIBUTES])
    if result.returncode != 0:
        raise CredentialStoreError("Unable to clear the Antigravity credential from Linux Secret Service.")
    if read_linux_live_credential(required=False) is not None:
        raise CredentialStoreError("Linux Secret Service credential remained after clear.")
    return True


def restore_linux_live_credential(blob: bytes | None) -> None:
    if blob is None:
        clear_linux_live_credential()
    else:
        write_linux_live_credential(blob)

"""Windows Credential Manager backend for saved agy profiles.

Generic targets: ``gemini:antigravity`` (live) and ``agy-cli-manager:<name>``
(saved accounts). Disk under accounts/<name>/ holds non-secret metadata only.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT_META_NAME = "credential.meta.json"
DEFAULT_LIVE_TARGET = "gemini:antigravity"
WINCRED_TARGET_ENV = "AGY_WINCRED_TARGET"
PROFILE_PREFIX = "agy-cli-manager:"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_BLOB_MAX = 2560
ERROR_NOT_FOUND = 1168
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260

_ADVAPI32 = None
_KERNEL32 = None


class CredentialStoreError(ValueError):
    """Raised when a credential operation cannot complete."""


class CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


def _check_blob(blob: bytes) -> None:
    if len(blob) > CRED_BLOB_MAX:
        raise CredentialStoreError(
            f"Credential blob is {len(blob)} bytes; CredWriteW limit is {CRED_BLOB_MAX} bytes"
        )


def _advapi32():
    global _ADVAPI32
    if _ADVAPI32 is not None:
        return _ADVAPI32
    if os.name != "nt":
        raise CredentialStoreError("Windows Credential Manager is only available on Windows")
    dll = ctypes.WinDLL("advapi32", use_last_error=True)
    dll.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
    ]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    dll.CredDeleteW.restype = wintypes.BOOL
    dll.CredEnumerateW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIALW))),
    ]
    dll.CredEnumerateW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    dll.CredFree.restype = None
    _ADVAPI32 = dll
    return dll


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    dll.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    dll.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    dll.Process32FirstW.restype = wintypes.BOOL
    dll.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    dll.Process32NextW.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [ctypes.c_void_p]
    dll.CloseHandle.restype = wintypes.BOOL
    _KERNEL32 = dll
    return dll


def _copy_blob(cred: CREDENTIALW) -> bytes:
    size = min(int(cred.CredentialBlobSize or 0), CRED_BLOB_MAX)
    if not cred.CredentialBlob or size <= 0:
        return b""
    return bytes(cred.CredentialBlob[:size])


def _cred_read(target: str) -> tuple[bytes, int, str]:
    dll = _advapi32()
    ptr = ctypes.POINTER(CREDENTIALW)()
    if not dll.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not ptr:
        raise CredentialStoreError(f"CredReadW returned a null credential for {target!r}")
    try:
        cred = ptr.contents
        blob = _copy_blob(cred)
        persist = int(cred.Persist or 0)
        username = str(cred.UserName or "")
        return blob, persist, username
    finally:
        dll.CredFree(ptr)


def _cred_write(target: str, blob: bytes, persist: int, username: str) -> None:
    payload = bytes(blob)
    _check_blob(payload)
    dll = _advapi32()
    keep: list[object] = []
    cred = CREDENTIALW()
    cred.Flags = 0
    cred.Type = CRED_TYPE_GENERIC
    target_buf = ctypes.create_unicode_buffer(target)
    keep.append(target_buf)
    cred.TargetName = ctypes.cast(target_buf, wintypes.LPWSTR)
    cred.Comment = None
    cred.LastWritten.dwLowDateTime = 0
    cred.LastWritten.dwHighDateTime = 0
    if payload:
        buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        keep.append(buf)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    else:
        cred.CredentialBlob = None
    cred.CredentialBlobSize = len(payload)
    cred.Persist = int(persist or CRED_PERSIST_LOCAL_MACHINE)
    user_buf = ctypes.create_unicode_buffer(username or "antigravity")
    keep.append(user_buf)
    cred.UserName = ctypes.cast(user_buf, wintypes.LPWSTR)
    cred.AttributeCount = 0
    cred.Attributes = None
    cred.TargetAlias = None
    if not dll.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    keep.append(cred)


def _cred_delete(target: str) -> bool:
    dll = _advapi32()
    if dll.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    err = ctypes.get_last_error()
    if err == ERROR_NOT_FOUND:
        return False
    raise ctypes.WinError(err)


def _is_not_found(exc: BaseException) -> bool:
    return getattr(exc, "winerror", None) == ERROR_NOT_FOUND


class WindowsCredentialStore:
    """ctypes wrapper around advapi32 generic credentials."""

    def __init__(self, target: str) -> None:
        if os.name != "nt":
            raise CredentialStoreError("WindowsCredentialStore is only available on Windows")
        name = str(target).strip()
        if not name:
            raise CredentialStoreError("Windows Credential Manager target name is empty")
        self.target_name = name

    def read(self) -> bytes:
        return _cred_read(self.target_name)[0]

    def envelope(self) -> tuple[bytes, int, str]:
        return _cred_read(self.target_name)

    def write(
        self,
        blob: bytes,
        *,
        persist: int = CRED_PERSIST_LOCAL_MACHINE,
        username: str = "antigravity",
    ) -> None:
        _cred_write(self.target_name, bytes(blob), persist, username)

    def delete(self) -> bool:
        return _cred_delete(self.target_name)

    def exists(self) -> bool:
        try:
            _cred_read(self.target_name)
        except OSError as exc:
            if _is_not_found(exc):
                return False
            raise
        return True


def enumerate_targets(filter_name: str) -> list[str]:
    """Return matching target names only; never include blob bytes."""
    dll = _advapi32()
    count = wintypes.DWORD(0)
    creds = ctypes.POINTER(ctypes.POINTER(CREDENTIALW))()
    ok = dll.CredEnumerateW(filter_name, 0, ctypes.byref(count), ctypes.byref(creds))
    if not ok:
        err = ctypes.get_last_error()
        if err == ERROR_NOT_FOUND:
            return []
        raise ctypes.WinError(err)
    try:
        names: list[str] = []
        for index in range(int(count.value)):
            names.append(str(creds[index].contents.TargetName or ""))
        return names
    finally:
        if creds:
            dll.CredFree(creds)


def profile_target(name: str) -> str:
    return f"{PROFILE_PREFIX}{name}"


def live_target() -> str:
    override = os.getenv(WINCRED_TARGET_ENV, "").strip()
    return override or DEFAULT_LIVE_TARGET


def copy_slot(
    source_target: str,
    dest_target: str,
    *,
    reuse_dest_envelope: bool = True,
) -> None:
    blob, persist, username = WindowsCredentialStore(source_target).envelope()
    dest = WindowsCredentialStore(dest_target)
    if reuse_dest_envelope and dest.exists():
        _, persist, username = dest.envelope()
    dest.write(blob, persist=persist, username=username)


def apply_to_live(
    source_target: str,
    dest_target: str | None = None,
    *,
    force: bool = False,
) -> None:
    dest = live_target() if dest_target is None else dest_target
    if dest == DEFAULT_LIVE_TARGET:
        assert_live_slot_idle(dest, force=force)
    copy_slot(source_target, dest, reuse_dest_envelope=True)


def list_conflicting_live_processes() -> list[str]:
    """Return running agy.exe / Antigravity* process labels (name + pid)."""
    if os.name != "nt":
        return []
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap:
        raise CredentialStoreError(
            f"CreateToolhelp32Snapshot failed (winerror={ctypes.get_last_error()})"
        )
    matches: list[str] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = bool(k32.Process32FirstW(snap, ctypes.byref(entry)))
        while ok:
            name = str(entry.szExeFile or "")
            lower = name.lower()
            if lower in {"agy.exe", "agy"} or lower.startswith("antigravity"):
                matches.append(f"{name} pid={int(entry.th32ProcessID)}")
            ok = bool(k32.Process32NextW(snap, ctypes.byref(entry)))
        return matches
    finally:
        k32.CloseHandle(snap)


def assert_live_slot_idle(target_name: str | None = None, *, force: bool = False) -> None:
    """Refuse capture/activate of the real live CredMan slot while agy is running."""
    target = (live_target() if target_name is None else target_name).strip()
    if target != DEFAULT_LIVE_TARGET or force:
        return
    running = list_conflicting_live_processes()
    if running:
        raise CredentialStoreError(
            "Refusing live Credential Manager capture/activate while these processes "
            f"are running: {', '.join(running)}. Exit them or pass --force to override."
        )


def _legacy_json_path(account_dir: Path) -> Path | None:
    for candidate in (
        account_dir / "wincred_antigravity.json",
        account_dir / ".gemini" / "wincred_antigravity.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def _write_public_meta(account_dir: Path, name: str, blob: bytes, target: str) -> Path:
    dest_dir = account_dir.parent if account_dir.name == ".gemini" else account_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / ACCOUNT_META_NAME
    meta = {
        "profile_name": name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "blob_sha256": hashlib.sha256(blob).hexdigest(),
        "blob_length": len(blob),
        "TargetName": target,
        "UserName": "antigravity",
        "Persistence": CRED_PERSIST_LOCAL_MACHINE,
        "Type": CRED_TYPE_GENERIC,
        "Flags": 0,
        "Comment": None,
        "TargetAlias": "",
        "Attributes": [],
    }
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def migrate_legacy_json(account_dir: Path, name: str) -> bool:
    source = _legacy_json_path(Path(account_dir))
    if source is None:
        return False
    target = profile_target(name)
    store = WindowsCredentialStore(target)
    if store.exists():
        return False
    blob = source.read_bytes()
    store.write(blob)
    _write_public_meta(Path(account_dir), name, blob, target)
    source.unlink(missing_ok=True)
    return True

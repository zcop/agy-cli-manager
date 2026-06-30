"""agy-cli-manager package."""

from agy_cli_manager.manager import (
    ManagerPaths,
    RotationResult,
    apply_active,
    build_paths,
    default_root,
    ensure_layout,
    get_status_snapshot,
    refresh_account_usage,
    rotate_after_failure,
    set_live_dir,
    switch_account,
    switch_next,
    update_account_runtime_metadata,
)

__all__ = [
    "ManagerPaths",
    "RotationResult",
    "apply_active",
    "build_paths",
    "default_root",
    "ensure_layout",
    "get_status_snapshot",
    "refresh_account_usage",
    "rotate_after_failure",
    "set_live_dir",
    "switch_account",
    "switch_next",
    "update_account_runtime_metadata",
]

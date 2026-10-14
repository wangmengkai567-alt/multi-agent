"""Compatibility exports for the shared, strict path policy."""

from utils.secure_paths import (
    PathDenied,
    confined_path,
    resolve_path,
    resolve_run_path,
    validate_filename,
    validate_identifier,
)

__all__ = [
    "PathDenied",
    "confined_path",
    "resolve_path",
    "resolve_run_path",
    "validate_filename",
    "validate_identifier",
]

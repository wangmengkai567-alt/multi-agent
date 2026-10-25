"""Shared containment rules for tools, uploads, downloads and static files."""

import re
import stat
from pathlib import Path, PureWindowsPath


class PathDenied(PermissionError):
    pass


def resource_path(value, root):
    """Public file references are strictly relative to a server-owned resource root."""
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")) or PureWindowsPath(value).drive:
        raise PathDenied("A relative resource path is required")
    for component in value.replace("\\", "/").split("/"):
        validate_filename(component)
    return confined_path(value, root)


def validate_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise PathDenied("Invalid session identifier")
    return value


def validate_filename(value):
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise PathDenied("Invalid filename")
    if any(ord(char) < 32 for char in value) or any(char in value for char in '/\\:*?"<>|'):
        raise PathDenied("Filename must not contain a path or Windows special characters")
    if value[-1] in (" ", ".") or re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value):
        raise PathDenied("Reserved filename")
    return value


def _check_reparse(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PathDenied("Symlinks and junctions are not allowed")


def confined_path(value, root, *, virtual=False):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PathDenied("Invalid file path")
    normalized = value.replace("\\", "/")
    if ".." in normalized.split("/") or normalized.startswith("~"):
        raise PathDenied("Path traversal is not allowed")
    win = PureWindowsPath(normalized)
    if normalized.startswith("//") or (win.drive and not win.is_absolute()):
        raise PathDenied("UNC and drive-relative paths are not allowed")
    root = Path(root).absolute()
    _check_reparse(root)
    for ancestor in root.parents:
        _check_reparse(ancestor)
    path = Path(normalized)
    if virtual and not win.drive:
        candidate = root / normalized.lstrip("/")
    else:
        candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.absolute().relative_to(root)
    except ValueError as exc:
        raise PathDenied("File path is outside the allowed directory") from exc
    current = root
    for component in relative.parts:
        if component == ".":
            continue
        validate_filename(component)
        current /= component
        _check_reparse(current)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PathDenied("Resolved file path is outside the allowed directory")
    return resolved


def iter_confined_files(root):
    root = Path(root).absolute()
    pending = [confined_path(".", root)]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            try:
                child = confined_path(str(child), root)
                if child.is_dir():
                    pending.append(child)
                elif child.is_file():
                    yield child
            except (PathDenied, OSError):
                continue


def resolve_run_path(filename, *, write=False, virtual=False):
    from agent.runtime.context import get_run_context
    from agent.runtime.policies import PROJECT_ROOT, policy_for_run
    from agent.runtime.skills import authorize_skill_path

    context = get_run_context()
    if context is None:
        raise PathDenied("File operations require a run context")
    if not isinstance(filename, str):
        raise PathDenied("Invalid file path")
    normalized = filename.replace("\\", "/")
    skill_dir = context.skill_dir or PROJECT_ROOT / policy_for_run().files.skill_directory
    physical = Path(normalized)
    if physical.is_absolute() and physical.is_relative_to(Path(skill_dir).absolute()):
        if write:
            raise PathDenied("Skill files are read-only")
        return authorize_skill_path(confined_path(str(physical), skill_dir), skill_dir, directories=True)
    if normalized.startswith(("/skills/", "skills/")) or normalized in ("/skills", "skills"):
        if write:
            raise PathDenied("Skill files are read-only")
        suffix = normalized.lstrip("/")[len("skills") :].lstrip("/") or "."
        return authorize_skill_path(confined_path(suffix, skill_dir), skill_dir, directories=True)
    prefix = f"output/session_{context.thread_id}/run_{context.run_id}"
    if normalized == prefix:
        normalized = "."
    elif normalized.startswith(prefix + "/"):
        normalized = normalized[len(prefix) + 1 :]
    for alias in ("/workspace/", "/mnt/data/", "/home/user/"):
        if normalized.startswith(alias):
            normalized = normalized[len(alias) :]
            break
    candidate = confined_path(normalized, context.work_dir, virtual=virtual)
    if write and candidate.is_relative_to((context.work_dir / "results").resolve()):
        raise PathDenied("Saved query results are read-only")
    if write and candidate.is_relative_to((context.work_dir / "uploads").resolve()):
        raise PathDenied("Uploaded source files are read-only")
    return candidate


def resolve_path(filename: str, session_dir: str | None = None, *, write=False) -> str:
    from agent.runtime.context import get_run_context

    if get_run_context() is not None:
        return str(resolve_run_path(filename, write=write))
    if session_dir is None:
        raise PathDenied("File operations require an explicit allowed directory")
    return str(confined_path(filename, session_dir))

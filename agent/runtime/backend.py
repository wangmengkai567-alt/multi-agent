"""Run-scoped filesystem backend with a separate read-only Skill mount."""

from fnmatch import fnmatch
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult

from agent.runtime.context import get_run_context
from agent.runtime.policies import PROJECT_ROOT, policy_for_run
from utils.path_utils import PathDenied, confined_path
from utils.secure_paths import iter_confined_files


class GuardedFilesystemBackend(FilesystemBackend):
    def __init__(self, root, *, readonly=False):
        super().__init__(root_dir=root, virtual_mode=True)
        self.readonly = readonly

    def _resolve_path(self, key):
        return confined_path(key, self.cwd, virtual=True)

    def _write_denied(self, key):
        if self.readonly:
            return True
        path = self._resolve_path(key)
        relative = path.relative_to(self.cwd).parts
        return bool(relative and relative[0].lower() in ("uploads", "skills", "results"))

    def write(self, file_path, content):
        if self._write_denied(file_path):
            return WriteResult(error="Directory is read-only")
        return super().write(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        if self._write_denied(file_path):
            return EditResult(error="Directory is read-only")
        return super().edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files):
        responses = []
        for path, content in files:
            try:
                if self._write_denied(path):
                    responses.append(FileUploadResponse(path=path, error="permission_denied"))
                else:
                    responses.extend(super().upload_files([(path, content)]))
            except PathDenied:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
        return responses

    def _safe_files(self, path):
        base = self._resolve_path(path or "/")
        candidates = [base] if base.is_file() else iter_confined_files(base) if base.exists() else []
        for candidate in candidates:
            try:
                checked = confined_path(str(candidate), self.cwd)
                if checked.is_file():
                    yield checked
            except (PathDenied, OSError):
                continue

    def glob_info(self, pattern, path="/"):
        if ".." in pattern.replace("\\", "/").split("/") or ":" in pattern or pattern.startswith(("/", "\\")):
            raise PathDenied("Invalid glob pattern")
        base = self._resolve_path(path)
        return [
            {"path": self._to_virtual_path(file), "is_dir": False, "size": file.stat().st_size}
            for file in self._safe_files(path)
            if fnmatch(file.relative_to(base).as_posix(), pattern)
            or (pattern.startswith("**/") and fnmatch(file.relative_to(base).as_posix(), pattern[3:]))
        ]

    def grep_raw(self, pattern, path=None, glob=None):
        results = []
        for file in self._safe_files(path):
            if glob and not fnmatch(file.name, glob):
                continue
            if file.stat().st_size > self.max_file_size_bytes:
                continue
            try:
                for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern in line:
                        results.append({"path": self._to_virtual_path(file), "line": number, "text": line})
            except (OSError, UnicodeError):
                continue
        return results


def run_backend(runtime):
    from agent.runtime.skills import SkillBackend, skill_scope

    context = get_run_context()
    if context is None:
        raise PathDenied("A run context is required for the filesystem backend")
    limits = policy_for_run()
    skill_dir = context.skill_dir or PROJECT_ROOT / limits.files.skill_directory
    return CompositeBackend(
        default=GuardedFilesystemBackend(confined_path(".", context.work_dir)),
        routes={"/skills/": SkillBackend(confined_path(".", Path(skill_dir)), skill_scope.get()[1])},
    )

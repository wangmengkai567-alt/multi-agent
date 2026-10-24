"""Run-scoped filesystem backend with a separate read-only Skill mount."""

from fnmatch import fnmatch
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)

from agent.runtime.context import get_run_context
from agent.runtime.policies import PROJECT_ROOT, policy_for_run
from utils.path_utils import PathDenied, confined_path
from utils.secure_paths import iter_confined_files


class GuardedFilesystemBackend(BackendProtocol):
    def __init__(self, root, *, readonly=False):
        self.cwd = Path(root).resolve()
        self.delegate = FilesystemBackend(root_dir=self.cwd, virtual_mode=True)
        self.max_file_size_bytes = 10 * 1024 * 1024
        self.readonly = readonly

    def resolve_path(self, key):
        return confined_path(key, self.cwd, virtual=True)

    def virtual_path(self, path):
        return "/" + Path(path).relative_to(self.cwd).as_posix()

    def ls_info(self, path):
        checked = self.resolve_path(path)
        rows = self.delegate.ls_info(self.virtual_path(checked))
        result = []
        for row in rows:
            try:
                self.resolve_path(row["path"])
                result.append(row)
            except PathDenied:
                continue
        return result

    def read(self, file_path, offset=0, limit=2000):
        path = self.resolve_path(file_path)
        return self.delegate.read(self.virtual_path(path), offset=offset, limit=limit)

    def download_files(self, paths):
        results = []
        for path in paths:
            try:
                file = self.resolve_path(path)
                results.extend(self.delegate.download_files([self.virtual_path(file)]))
            except PathDenied:
                results.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
        return results

    def _write_denied(self, key):
        if self.readonly:
            return True
        path = self.resolve_path(key)
        relative = path.relative_to(self.cwd).parts
        return bool(relative and relative[0].lower() in ("uploads", "skills", "results"))

    def write(self, file_path, content):
        if self._write_denied(file_path):
            return WriteResult(error="Directory is read-only")
        return self.delegate.write(self.virtual_path(self.resolve_path(file_path)), content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        if self._write_denied(file_path):
            return EditResult(error="Directory is read-only")
        return self.delegate.edit(self.virtual_path(self.resolve_path(file_path)), old_string, new_string, replace_all)

    def upload_files(self, files):
        responses = []
        for path, content in files:
            try:
                if self._write_denied(path):
                    responses.append(FileUploadResponse(path=path, error="permission_denied"))
                else:
                    responses.extend(
                        self.delegate.upload_files([(self.virtual_path(self.resolve_path(path)), content)])
                    )
            except PathDenied:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
        return responses

    def _safe_files(self, path):
        base = self.resolve_path(path or "/")
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
        base = self.resolve_path(path)
        return [
            {"path": self.virtual_path(file), "is_dir": False, "size": file.stat().st_size}
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
                        results.append({"path": self.virtual_path(file), "line": number, "text": line})
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

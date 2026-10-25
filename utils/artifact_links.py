"""Artifact links identify an authorized run and a relative file, never a disk path."""

from urllib.parse import quote

from utils.secure_paths import validate_filename, validate_identifier


def artifact_url(run_id, relative):
    validate_identifier(run_id)
    for component in relative.split("/"):
        validate_filename(component)
    return f"/api/runs/{quote(run_id, safe='')}/files/download?path={quote(relative, safe='')}"

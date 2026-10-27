"""Atomic UTF-8/CRLF execution evidence files; no background checkpoint recovery."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_json(path, value):
    from agent.results import clean

    write_state_json(path, clean(value))


def write_state_json(path, value):
    """Persist exact internal state; redaction must never change digests or recovery inputs."""
    path = Path(path).resolve()
    import os

    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        path = Path("\\\\?\\" + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    raw = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n"
    try:
        with NamedTemporaryFile(mode="wb", prefix=".write-", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw.encode("utf-8"))
            stream.flush()
            import os

            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

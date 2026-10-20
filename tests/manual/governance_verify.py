"""Capture verification output using explicit UTF-8/CRLF, without shell redirection."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "runtime-governance-validation"


def write(name, text):
    (OUT / name).write_bytes((text.replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8"))


def main():
    OUT.mkdir(exist_ok=True)
    commands = [
        (
            "pytest-full.log",
            [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "tests",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp=" + str(OUT / "pytest-full"),
            ],
        ),
        ("frontend-app.log", ["node", "tests/frontend/app.test.cjs"]),
        ("frontend-events.log", ["node", "tests/frontend/run-events.test.cjs"]),
        ("frontend-groups.log", ["node", "tests/frontend/call-groups.test.cjs"]),
    ]
    results = []
    for name, command in commands:
        print("RUN", name, flush=True)
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        text = result.stdout + result.stderr
        write(name, text)
        results.append(dict(log=name, exit_code=result.returncode))
        print(name, "EXIT", result.returncode, text[-3000:], flush=True)
    write("checks.json", json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    return 0 if all(r["exit_code"] == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

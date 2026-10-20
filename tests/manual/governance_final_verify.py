"""Freeze hashes and run the full offline suite in independent, balanced processes."""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = (
    ROOT / "output" / "runtime-governance-validation" / ("final-checks-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
)


def write(name, text):
    (OUT / name).write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


def hashes():
    result = {}
    for directory in ("agent", "api", "tools", "utils", "config", "prompt", "tests"):
        for path in (ROOT / directory).rglob("*"):
            if (
                path.is_file()
                and path.suffix in (".py", ".js", ".cjs", ".yml", ".html")
                and "__pycache__" not in path.parts
                and "vendor" not in path.parts
            ):
                result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def run(name, command):
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=600,
    )
    text = result.stdout + result.stderr
    write(name, text)
    record = dict(log=name, exit_code=result.returncode, tail=text[-2500:])
    print(name, "EXIT", result.returncode, text[-2500:], flush=True)
    return record


def main():
    OUT.mkdir(parents=True)
    before = hashes()
    write("source-hashes.json", json.dumps(before, indent=2) + "\n")
    collected = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    write("collection.log", collected.stdout + collected.stderr)
    if collected.returncode:
        return collected.returncode
    nodes = [line for line in collected.stdout.splitlines() if "::" in line and line.startswith("tests/")]
    groups = [nodes[index::6] for index in range(6)]
    loads = [len(group) for group in groups]
    print("COLLECTED", sum(loads), "GROUPS", loads, "DIRECTORY", OUT, flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(
                run,
                f"pytest-group-{index + 1}.log",
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pytest",
                    *files,
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp=" + str(OUT / f"temp-{index + 1}"),
                ],
            )
            for index, files in enumerate(groups)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    for name in ("app", "run-events", "call-groups"):
        results.append(run(f"frontend-{name}.log", ["node", f"tests/frontend/{name}.test.cjs"]))
    after = hashes()
    unchanged = before == after
    report = dict(collected=sum(loads), group_counts=loads, source_unchanged=unchanged, results=results)
    write("summary.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("FINAL", OUT, "UNCHANGED", unchanged, flush=True)
    return 0 if unchanged and all(r["exit_code"] == 0 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

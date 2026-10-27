"""Run the isolated offline suites and retain every failure with its first blocker."""

import argparse
import json
import os
import platform
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def write_text(path, text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    path.write_bytes(normalized.encode("utf-8"))


def classify(text):
    return dict(
        category="needs_investigation",
        blocker="unreviewed",
        source=None,
        action="Inspect this failure against the current execution contract; no failure is waived.",
    )


def pytest_results(path):
    document = ElementTree.parse(path)
    cases = []
    for case in document.iter("testcase"):
        row = dict(name=case.get("name"), module=case.get("classname"), seconds=float(case.get("time", "0")))
        outcomes = [child for child in case if child.tag in {"failure", "error", "skipped"}]
        row["status"] = outcomes[0].tag if outcomes else "passed"
        row["outcomes"] = []
        for item in outcomes:
            detail = dict(status=item.tag, message=item.get("message", ""), trace=item.text or "")
            if item.tag != "skipped":
                detail["first_blocker"] = classify(detail["message"] + "\n" + detail["trace"])
            row["outcomes"].append(detail)
        cases.append(row)
    return cases


def run_step(name, command, directory, environment, timeout):
    print(f"START {name}", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output, code = result.stdout.decode("utf-8"), result.returncode
    except subprocess.TimeoutExpired as error:
        output, code = (error.stdout or b"").decode("utf-8") + "\nVerification command timed out.\n", 124
    except OSError as error:
        output, code = f"Cannot start verification command: {error}\n", 127
    write_text(directory / (name + ".log"), output)
    print(f"FINISH {name} exit={code}", flush=True)
    return dict(name=name, command=command, exit_code=code, log=name + ".log")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", type=Path, required=True, help="A new directory beneath this project's output directory."
    )
    parser.add_argument(
        "--node", default="node", help="Explicit Node executable; missing executables fail verification."
    )
    parser.add_argument("--uv", default="uv", help="Explicit uv executable for the dependency consistency check.")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument(
        "--baseline-report", type=Path, help="Prior report.json to compare every case without waiving failures."
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    directory = args.report_dir.resolve()
    directory.relative_to((ROOT / "output").resolve())
    if directory == (ROOT / "output").resolve():
        parser.error("Use a new subdirectory beneath output")
    directory.mkdir(parents=True, exist_ok=False)
    environment = dict(
        os.environ,
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHON_DOTENV_DISABLED="1",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
    )
    for key in tuple(environment):
        if key.startswith(("LLM_", "OPENAI_", "ANTHROPIC_", "TAVILY_", "MYSQL_", "APP_")) or key == "LANGSMITH_API_KEY":
            environment.pop(key)
    python = [sys.executable, "-X", "utf8", "-B", "-m"]
    steps = [
        ("dependencies", [args.uv, "pip", "check", "--python", sys.executable]),
        ("lint", [*python, "ruff", "check", "."]),
        ("format", [*python, "ruff", "format", "--check", "."]),
        ("pytest", [*python, "pytest", "tests", "-q", "--tb=short", "--junitxml=" + str(directory / "pytest.xml")]),
        (
            "frontend",
            [args.node, "--test", *[str(path) for path in sorted((ROOT / "tests" / "frontend").glob("*.test.cjs"))]],
        ),
    ]
    results = [run_step(name, command, directory, environment, args.timeout_seconds) for name, command in steps]
    cases = pytest_results(directory / "pytest.xml") if (directory / "pytest.xml").is_file() else []
    # A missing report or an empty collection can never become success.
    passed = (
        bool(cases)
        and all(step["exit_code"] == 0 for step in results)
        and not any(case["status"] in {"failure", "error"} for case in cases)
    )
    counts = dict(Counter(case["status"] for case in cases))
    blockers = Counter(
        outcome["first_blocker"]["category"]
        for case in cases
        for outcome in case["outcomes"]
        if "first_blocker" in outcome
    )
    comparison = None
    if args.baseline_report is not None:
        baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
        before = {(row["module"], row["name"]): row for row in baseline["cases"]}
        after = {(row["module"], row["name"]): row for row in cases}
        comparison = dict(
            baseline=str(args.baseline_report.resolve()),
            before_counts=baseline["pytest_counts"],
            after_counts=counts,
            transitions=[
                dict(module=key[0], name=key[1], before=before[key]["status"], after=after[key]["status"])
                for key in sorted(before.keys() & after.keys())
                if before[key]["status"] != after[key]["status"]
            ],
            removed=[dict(module=key[0], name=key[1]) for key in sorted(before.keys() - after.keys())],
            added=[dict(module=key[0], name=key[1]) for key in sorted(after.keys() - before.keys())],
        )
    report = dict(
        baseline_comparison=comparison,
        passed=passed,
        created_at=datetime.now(UTC).isoformat(),
        python=sys.version,
        platform=platform.platform(),
        versions={
            name: version(name) for name in ("pytest", "pytest-asyncio", "ruff", "deepagents", "langchain", "langgraph")
        },
        steps=results,
        pytest_counts=counts,
        first_blocker_counts=dict(blockers),
        cases=cases,
    )
    write_text(directory / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = [
        "# Offline verification",
        "",
        f"Passed: {passed}",
        "",
        "First-blocker labels do not waive failures or prove downstream behavior.",
        "",
        "| Step | Exit code | Log |",
        "| --- | ---: | --- |",
    ]
    lines.extend(f"| {step['name']} | {step['exit_code']} | [{step['log']}]({step['log']}) |" for step in results)
    lines.extend(["", "## Pytest", "", json.dumps(counts)])
    if comparison is not None:
        lines.extend(
            [
                "",
                "## Baseline comparison",
                "",
                json.dumps(
                    {key: value for key, value in comparison.items() if key != "transitions"}, ensure_ascii=False
                ),
                "",
                "All renamed/added/removed cases must be reviewed; a transition never waives a failure.",
                "",
            ]
        )
    lines.extend(["", "| Case | Status | First blocker |", "| --- | --- | --- |"])
    for case in cases:
        if case["status"] == "passed":
            continue
        labels = ", ".join(item["first_blocker"]["blocker"] for item in case["outcomes"] if "first_blocker" in item)
        name = (case["module"] + "::" + case["name"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {case['status']} | {labels} |")
    lines.extend(["", "Full traces and attribution rules are retained in report.json and pytest.xml.", ""])
    write_text(directory / "report.md", "\n".join(lines))
    print(
        json.dumps(
            dict(
                passed=passed,
                pytest_counts=counts,
                first_blocker_counts=dict(blockers),
                report=str(directory / "report.md"),
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

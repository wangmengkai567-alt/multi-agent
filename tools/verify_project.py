"""Run the unchanged offline suites and retain every failure with its first blocker."""

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

# These identify the observed first blocker, never a passing or waived test.
BLOCKERS = (
    (
        "initialization",
        "security_migration",
        "Security database migration required",
        "api/run_store.py",
        "Fixture must explicitly migrate its temporary databases before opening the runtime.",
    ),
    (
        "initialization",
        "authenticated_lifespan",
        "has no attribute 'auth'",
        "api/security.py",
        "ASGI fixture must enter the authenticated lifespan and log in before protected requests.",
    ),
    (
        "old_contract",
        "quote_registration",
        "quote_id: Field required",
        "agent/runtime/quotes.py",
        "Report fixtures must use runtime-owned quote IDs and the current evidence protocol.",
    ),
    (
        "old_contract",
        "quote_registration",
        "Quote registration requires an owned delegated task",
        "agent/runtime/quotes.py",
        "Fixture must establish its actual delegated task owner before report parsing.",
    ),
    (
        "old_contract",
        "evidence_record",
        "Unsupported tool evidence protocol",
        "agent/runtime/evidence_protocol.py",
        "Raw legacy evidence dictionaries do not satisfy the versioned evidence contract.",
    ),
    (
        "old_contract",
        "tool_binding",
        "explicitly versioned evidence adapter is required",
        "agent/runtime/evidence_protocol.py",
        "Registered fixture tools must declare their evidence adapter.",
    ),
    (
        "old_contract",
        "tool_binding",
        "Executed evidence has no declared adapter",
        "agent/runtime/evidence_protocol.py",
        "The simulated executed tool call omits its mandatory evidence binding.",
    ),
    (
        "old_contract",
        "route_decision",
        "validation errors for RouteDecision",
        "agent/runtime/contracts.py",
        "Fixture must provide the complete current intent/material/permission contract.",
    ),
    (
        "old_contract",
        "job_ownership",
        "JobService.open() missing",
        "agent/runtime/jobs.py",
        "JobService requires the shared scheduler and migrated parent run store.",
    ),
    (
        "old_contract",
        "expert_review",
        "explicit enabled semantic review policy",
        "agent/subagent_registry.py",
        "Expert definitions must declare the mandatory semantic review policy.",
    ),
    (
        "old_contract",
        "removed_conversion",
        "No module named 'utils.word_converter'",
        "docs/implementation/34-remove-format-conversion.md",
        "PDF export was explicitly removed; do not restore its implementation for obsolete checks.",
    ),
    (
        "old_contract",
        "removed_conversion",
        "No module named 'agent.runtime.artifact_jobs'",
        "docs/implementation/34-remove-format-conversion.md",
        "Background conversion was explicitly removed.",
    ),
    (
        "old_contract",
        "provider_process",
        "has no attribute 'connect'",
        "agent/runtime/worker.py",
        "Database connections are owned by the managed provider process, not tools.db_tools.",
    ),
    (
        "old_contract",
        "provider_process",
        "has no attribute 'TavilyClient'",
        "agent/runtime/worker.py",
        "Tavily clients are owned by the managed provider process.",
    ),
)


def write_text(path, text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    path.write_bytes(normalized.encode("utf-8"))


def classify(text):
    for category, blocker, marker, source, action in BLOCKERS:
        if marker in text:
            return dict(category=category, blocker=blocker, source=source, action=action)
    return dict(
        category="needs_investigation",
        blocker="unreviewed",
        source=None,
        action="Inspect this assertion and its fixture against the current implementation; no automatic attribution.",
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
    parser.add_argument("--timeout-seconds", type=int, default=300)
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
    report = dict(
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
    lines.extend(
        ["", "## Pytest", "", json.dumps(counts), "", "| Case | Status | First blocker |", "| --- | --- | --- |"]
    )
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

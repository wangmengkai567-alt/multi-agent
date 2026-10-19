"""Run diverse real-provider cases in isolated processes; keep functional and runtime verdicts separate."""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = ("arithmetic", "weather", "official_docs", "four_objects", "unknown_entity", "watch_comparison")


def evaluate(case, record):
    answer = record.get("answer") or ""
    checks = record.get("checks", {})
    runtime = dict(record.get("flow_checks", {}))
    runtime["clean_output"] = not any(
        marker in answer for marker in ("</think>", "<think>", "<tool_call>", "<object object at")
    )
    runtime["no_internal_envelope"] = not (answer.lstrip().startswith("{") and '"failed_checks"' in answer)
    runtime["neutral_routing"] = checks.get("renamed_search", False) and checks.get("no_ecommerce_skill", False)
    runtime["has_source_records"] = all(
        checks.get(key, False) for key in ("bounded_searches", "task_budgets_recorded", "each_task_got_search")
    )
    functional = dict(answer_present=bool(answer.strip()), no_database=checks.get("no_database", False))
    if case == "arithmetic":
        functional["correct_result"] = "391" in answer
        functional["no_search"] = record.get("search_calls") == 0 and not record.get("subtasks")
    elif case == "unknown_entity":
        functional["actual_verification_attempt"] = record.get("search_calls", 0) > 0
        functional["exact_model_preserved"] = "VERIFY-NONEXISTENT-MODEL-20261018-72AF91" in answer
        functional["explicit_uncertainty"] = bool(
            re.search(
                r"无法.{0,12}(?:确认|核实|证实)|未(?:能|检索到|查到|找到|核实)|没有.{0,8}(?:可靠|官方|确切)", answer
            )
        )
        functional["no_invented_price"] = not bool(re.search(r"(?:官方售价|起售价|定价)[：:为是 ]*[¥￥]?\d+", answer))
    else:
        functional["approved_source_task"] = any(t.get("check_status") == "passed" for t in record.get("subtasks", []))
        final_check = record.get("final_check") or {}
        functional["complete_answer"] = (
            final_check.get("status") == "passed" and final_check.get("data", {}).get("answer_kind") == "complete"
        )
        functional["final_answer_checked"] = checks.get("final_answer_checked", False)
        if case in ("weather", "official_docs", "four_objects"):
            functional["source_url_in_answer"] = bool(re.search(r"https?://[^\s)<>]+", answer))
        if case == "official_docs":
            functional["official_source"] = "docs.python.org/" in answer
            functional["topic_answered"] = "TaskGroup" in answer and ("取消" in answer or "cancel" in answer.lower())
        if case == "four_objects":
            functional["all_objects"] = all(term in answer.lower() for term in ("list", "tuple", "set", "dict"))
        if case == "weather":
            reference = (
                datetime.fromisoformat(record["reference_time"])
                if record.get("reference_time")
                else datetime.now().astimezone()
            )
            tomorrow = reference.date() + timedelta(days=1)
            functional["forecast_date"] = tomorrow.isoformat() in answer or bool(
                re.search(rf"{tomorrow.month}\s*月\s*{tomorrow.day}\s*日", answer)
            )
    return dict(
        runtime=runtime,
        functional=functional,
        runtime_passed=all(runtime.values()),
        functional_passed=all(functional.values()),
    )


def run_case(case, directory):
    before = set((ROOT / "output/search-regression").glob("*/summary.json"))
    process = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "tests/manual/search_regression.py"), "--case", case],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=390,
    )
    candidates = []
    for path in set((ROOT / "output/search-regression").glob("*/summary.json")) - before:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("case") == case:
            candidates.append((path, data))
    if not candidates:
        return dict(
            case=case,
            exit_code=process.returncode,
            error="No completed regression record",
            runtime_passed=False,
            functional_passed=False,
        )
    path, record = max(candidates, key=lambda item: item[0].stat().st_mtime)
    evaluation = evaluate(case, record)
    result = dict(
        case=case,
        exit_code=process.returncode,
        elapsed_seconds=record.get("elapsed_seconds"),
        status=record.get("status"),
        record=str(path),
        **evaluation,
    )
    (directory / (case + ".json")).write_bytes(
        (json.dumps(result, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8")
    )
    return result


def main(cases):
    tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    directory = ROOT / "output/system-regression" / tag
    directory.mkdir(parents=True)
    print(json.dumps(dict(storage=str(directory), cases=cases)), flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run_case, case, directory): case for case in cases}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                result = dict(
                    case=futures[future], error=type(error).__name__, runtime_passed=False, functional_passed=False
                )
            results.append(result)
            (directory / "summary.json").write_bytes(
                (json.dumps(results, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8")
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if all(row["runtime_passed"] and row["functional_passed"] for row in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    args = parser.parse_args()
    raise SystemExit(main(args.cases))

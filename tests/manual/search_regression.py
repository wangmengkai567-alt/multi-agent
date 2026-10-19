"""Explicit real-provider regression using isolated storage and the production ASGI app.

Run from the project root: .venv/Scripts/python.exe tests/manual/search_regression.py
This uses the configured model and Tavily credentials; it is not collected by pytest.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main(case="weather"):
    import httpx

    import agent.main_agent as runtime
    import api.server as server

    tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    directory = ROOT / "output" / "search-regression" / tag
    directory.mkdir(parents=True)
    runtime.checkpoint_db_path = directory / "checkpoints.db"
    runtime.project_root_path = directory
    server.run_db_path = directory / "runs.db"
    server.output_dir = directory / "files"
    server.updated_dir = directory / "updated"
    # Record request controls only: never persist prompts, tool bodies, headers or credentials.
    from agent.llm import model
    from agent.runtime.search import search_allowance

    request_controls = []

    async def record_controls(request):
        payload = json.loads(request.content)
        allowance = search_allowance.get()
        request_controls.append(
            dict(
                timestamp=datetime.now(UTC).isoformat(),
                tool_choice=payload.get("tool_choice"),
                response_format=payload.get("response_format"),
                tool_count=len(payload.get("tools", [])),
                input_chars=sum(len(str(message.get("content", ""))) for message in payload.get("messages", [])),
                search_budget=allowance.snapshot() if allowance else None,
            )
        )

    model.root_async_client._client.event_hooks.setdefault("request", []).append(record_controls)
    question = {
        "weather": "明天武汉适合穿什么衣服？请写明预报日期并提供来源链接。",
        "arithmetic": "计算17乘以23，直接给出结果。",
        "official_docs": "请根据Python官方文档解释Python 3.11的asyncio.TaskGroup在一个子任务失败后怎样处理其余任务，附来源链接。",
        "four_objects": "请对比Python的list、tuple、set、dict的可变性、有序性、重复元素规则和典型用途。引用Python官方文档，不用生成文件。",
        "unknown_entity": "请核实型号 VERIFY-NONEXISTENT-MODEL-20261018-72AF91 的官方发布时间和参数。只接受这个精确型号；无法证实时明确说明，不能改成相似型号。",
        "comparison": "请你对比分析一下小米su7和尚界z7，哪个比较推荐？",
        "watch_comparison": "请你帮我对比一下华为 Watch fit4pro 和 华为 Watch fit5，哪一个更推荐购买？",
        "launch": "尚界Z7正式上市是哪一天？请核实发布会日期，附可核验来源。",
    }[case]
    reference_time = datetime.now().astimezone().isoformat()
    started = monotonic()
    async with server.lifespan(server.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://local-regression"
        ) as client:
            response = await client.post("/api/task", json={"query": question})
            response.raise_for_status()
            run_id = response.json()["run_id"]
            while True:
                record = (await client.get(f"/api/runs/{run_id}")).json()
                if record["status"] not in ("queued", "running"):
                    break
                if monotonic() - started > 330:
                    await client.post(f"/api/runs/{run_id}/cancel")
                    break
                await asyncio.sleep(1)
            history = (await client.get(f"/api/runs/{run_id}/history")).json()
            events = history["events"]
            search_events = [
                e for e in events if e["event"] == "before_tool" and e["actor"] == "search:internet_search"
            ]
            searches = {
                e["call_id"] for e in events if e["event"] == "before_tool" and e["actor"] == "search:internet_search"
            }
            skills = [e["data"].get("skill_id") for e in events if e["event"] == "skill_state"]
            tasks = [
                {key: row[key] for key in ("target", "display_name", "status", "check_status", "error")}
                for row in history["subtasks"]
            ]
            answer_events = [e for e in events if e["event"] == "answer_validation"]
            answer_checks = {e["call_id"]: e["status"] for e in answer_events}
            final_check = answer_events[-1] if answer_events else None
            expects_search = case != "arithmetic"
            checks = dict(
                completed=history["run"]["status"] == "completed",
                final_answer_checked=(not expects_search) or bool(final_check and final_check["status"] == "passed"),
                renamed_search=(bool(tasks) if expects_search else not tasks)
                and all(t["display_name"] == "联网搜索助手" for t in tasks),
                search_passed=(bool(tasks) if expects_search else not tasks)
                and all(t["check_status"] == "passed" for t in tasks),
                bounded_searches=(0 < len(searches) <= runtime.load_runtime_policy().search.calls_per_run)
                if expects_search
                else len(searches) == 0,
                task_budgets_recorded=(bool(search_events) if expects_search else not search_events)
                and all("search_budget" in e["data"] for e in search_events),
                each_task_got_search=(bool(tasks) if expects_search else not tasks)
                and all(
                    any(e["parent_call_id"] == task["call_id"] for e in search_events)
                    for task in history["subtasks"]
                    if task["target"] == "search"
                ),
                no_ecommerce_skill="ecommerce-analysis" not in skills,
                no_database=all(t["target"] == "search" for t in tasks),
            )
            if case == "comparison":
                # Request bodies are redacted in events; check the persisted actual request evidence below.
                actual_queries = []
                for row in history["results"]:
                    payload = (
                        json.loads(row["raw_text"])
                        if row.get("raw_text")
                        else json.loads(
                            (
                                server.output_dir
                                / f"session_{history['run']['thread_id']}"
                                / f"run_{run_id}"
                                / row["raw_path"]
                            ).read_text(encoding="utf-8")
                        )
                    )
                    for tool in payload["tools"]:
                        if tool["name"] == "internet_search" and tool["executed"]:
                            actual_queries.append(tool.get("request_parameters", {}).get("query", ""))
                queries = " ".join(actual_queries).lower()
                checks["both_objects_searched"] = "su7" in queries and "z7" in queries
                checks["no_model_date_cutoff_as_today"] = "截至 2026-02-20" not in (history["run"]["answer"] or "")
            if case == "launch":
                checks["launch_date_answered"] = bool(
                    re.search(r"4\s*(?:月|[-/.])\s*22", history["run"]["answer"] or "")
                )
            flow_checks = dict(
                final_answer=bool(history["run"]["answer"]) and history["run"]["status"] == "completed",
                no_cancel_object=all(
                    "<object object at" not in str(event.get("data", {}).get("error", "")) for event in events
                ),
                bounded_tasks=len(tasks) <= runtime.load_runtime_policy().search.max_tasks_per_run,
                no_timeout=history["run"]["status"] != "timed_out",
            )
            summary = dict(
                case=case,
                reference_time=reference_time,
                question=question,
                elapsed_seconds=round(monotonic() - started, 2),
                status=history["run"]["status"],
                search_calls=len(searches),
                skills=skills,
                subtasks=tasks,
                checks=checks,
                flow_checks=flow_checks,
                answer_checks=answer_checks,
                final_check=final_check,
                answer=history["run"]["answer"],
                error=history["run"]["error"],
                storage=str(directory),
                model_request_controls=request_controls,
            )
            output = json.dumps(summary, ensure_ascii=False, indent=2)
            (directory / "summary.json").write_bytes((output.replace("\n", "\r\n") + "\r\n").encode("utf-8"))
            print(output, flush=True)
            return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case",
        choices=(
            "weather",
            "comparison",
            "launch",
            "watch_comparison",
            "arithmetic",
            "official_docs",
            "four_objects",
            "unknown_entity",
        ),
        default="weather",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().case)))

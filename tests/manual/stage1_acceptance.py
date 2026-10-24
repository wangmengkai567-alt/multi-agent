"""Run the two user-approved questions against the real production app and providers."""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
QUESTIONS = [
    "请查看 Deep Agents、Open Deep Research 和 smolagents 的源码，分析它们如何进行子 agent 协作与工具调用，并结合我的项目提出改进建议。关键结论需要提供源码依据。",
    "不再联网，把上一轮的调研结果整理成一份实施计划，包含修改位置、依赖关系和验收标准。不要补充未经核实的新结论。",
]

STAGE2_QUESTIONS = [
    "请从 Deep Agents、Open Deep Research、smolagents 和 GPT Researcher 的官方源码与文档中，比较它们如何委派子任务、调用工具和返回结果，并结合我的项目提出改进建议。关键结论附源码链接；无法确认来源身份或缺少证据的地方明确说明，不要用推测补齐。",
    "研究 Open Deep Research 的子 agent 调度。先核实正常委派和结果返回流程，再根据实际源码发现的风险，追加调查必要的异常、取消或任务遗漏路径，最后给出有源码依据的修复建议。复用已取得的证据，不重复执行已经完成的调查；区分已确认问题和待核实疑点。",
]


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data
    path.write_bytes((text.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n").encode("utf-8"))


async def main():
    import httpx

    import agent.main_agent as runtime
    import api.server as server

    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("stage1", "stage2"), default="stage1")
    parser.add_argument("--resume", type=Path, help="Continue question 2 using question-1.json and its real database")
    args = parser.parse_args()
    if args.resume and args.stage != "stage1":
        raise ValueError("Only the original stage-one rewrite supports resuming its reviewed source")
    approved_questions = QUESTIONS if args.stage == "stage1" else STAGE2_QUESTIONS
    directory = (
        args.resume.resolve()
        if args.resume
        else (ROOT / "output" / f"{args.stage}-validation" / datetime.now().strftime("live-%Y%m%d-%H%M%S"))
    )
    if args.resume:
        directory.relative_to(ROOT / "output" / f"{args.stage}-validation")
        if not (directory / "question-1.json").is_file() or not (directory / "runs.db").is_file():
            raise ValueError("Resume requires the original question-1 record and run database")
    else:
        directory.mkdir(parents=True)
    runtime.checkpoint_db_path = directory / "checkpoints.db"
    runtime.project_root_path = directory
    server.run_db_path = directory / "runs.db"
    server.output_dir = directory / "files"
    server.updated_dir = directory / "updated"
    results = [json.loads((directory / "question-1.json").read_text(encoding="utf-8-sig"))] if args.resume else []
    questions = list(enumerate(approved_questions, 1))[len(results) :]
    async with server.lifespan(server.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://stage1.local"
        ) as client:
            for number, question in questions:
                started = monotonic()
                response = await client.post(
                    "/api/task",
                    json={
                        "query": question,
                        "thread_id": f"{args.stage}-business-session",
                        "execution_mode": "foreground",
                    },
                )
                response.raise_for_status()
                run_id = response.json()["run_id"]
                print(f"question_{number}_started run_id={run_id}", flush=True)
                execution = server.app.state.run_tasks.get(run_id)
                recording_error = None
                while True:
                    row = await server.app.state.run_store.get(run_id)
                    if row["status"] not in ("queued", "running"):
                        break
                    if execution is not None and execution.done():
                        recording_error = "Execution ended without a durable terminal record"
                        break
                    if monotonic() - started > server.app.state.runtime_policy.timeouts.total_seconds + 30:
                        recording_error = "Acceptance wait exceeded the configured execution deadline"
                        await client.post(f"/api/runs/{run_id}/cancel")
                        break
                    await asyncio.sleep(1)
                await server.app.state.event_sink.flush(run_id)
                events = await server.app.state.run_store.list_events(run_id)
                final = [event for event in events if event["event"] == "answer_validation"]
                contracts = [event for event in events if event["event"] == "task_contract"]
                requests = [event for event in events if event["event"] == "model_request"]
                searches = [
                    event
                    for event in events
                    if event["event"] == "research_attempt"
                    or event["actor"] == "search:research_step"
                    and event["event"] == "before_tool"
                ]
                contract = contracts[-1]["data"] if contracts else {}
                result = {
                    "question": question,
                    "run_id": run_id,
                    "status": row["status"],
                    "coverage": row.get("coverage"),
                    "elapsed_seconds": round(monotonic() - started, 2),
                    "contract": contract,
                    "search_actions": len(searches),
                    "model_requests": [event["data"] for event in requests],
                    "final_review_status": final[-1]["status"] if final else None,
                    "source_run_id": contract.get("source_run_id"),
                    "answer": row.get("answer"),
                    "error": row.get("error"),
                    "recording_error": recording_error,
                }
                result["process_passed"] = bool(
                    row["status"] == "completed"
                    and final
                    and final[-1]["status"] == "passed"
                    and contract.get("delivery_kind") == ("direct" if args.stage == "stage1" and number == 2 else "grounded")
                    and (args.stage == "stage2" or number == 1 or not searches and contract.get("source_run_id") == results[0]["run_id"])
                )
                result["business_passed"] = result["process_passed"] and (
                    row.get("coverage") == "complete" if args.stage == "stage2" or number == 1 else results[0].get("business_passed", False)
                )
                result["plan_updates"] = [event["data"] for event in events if event["event"] == "research_plan"]
                result["scheduling"] = [event["data"] for event in events if event["event"].startswith("scheduler_")]
                result["provider_errors"] = []
                for path in (directory / "files").rglob("results/tools/*.json"):
                    if run_id in str(path):
                        item = json.loads(path.read_text(encoding="utf-8-sig"))
                        if item.get("error"):
                            result["provider_errors"].append(item["error"])
                results.append(result)
                save(directory / f"question-{number}.json", result)
                save(directory / f"answer-{number}.md", row.get("answer") or "未获得交付")
                print(
                    f"question_{number}_finished status={row['status']} coverage={row.get('coverage')} process_passed={result['process_passed']}",
                    flush=True,
                )
    save(
        directory / "summary.json",
        {
            "questions": results,
            "process_passed": all(r["process_passed"] for r in results),
            "business_passed": all(r.get("business_passed", r.get("coverage") == "complete") for r in results),
            "semantic_review": "Read answers and source links; process checks do not grade writing style.",
        },
    )
    print("evidence_directory=" + str(directory), flush=True)
    return 0 if all(r.get("business_passed", r.get("coverage") == "complete") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

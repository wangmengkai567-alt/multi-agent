"""Real-provider acceptance, isolated from user runs. No listener, Git operation or history rewrite."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def write(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n").encode())


async def main():
    import httpx

    import agent.main_agent as runtime
    import api.server as server

    tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
    directory = ROOT / "output" / "runtime-governance-validation" / tag
    directory.mkdir(parents=True)
    runtime.checkpoint_db_path = directory / "checkpoints.db"
    runtime.project_root_path = directory
    server.run_db_path = directory / "runs.db"
    server.output_dir = directory / "files"
    server.updated_dir = directory / "updated"
    start = monotonic()
    async with server.lifespan(server.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://acceptance"
        ) as client:
            response = await client.post(
                "/api/task",
                json={
                    "query": "请你分析一下山西晋城和河南新乡哪一个城市更宜居，适合人生活？请附实际来源，必要数据未核实请明确说明。",
                    "execution_mode": "background",
                },
            )
            response.raise_for_status()
            receipt = response.json()
            print(
                "RECEIPT", json.dumps(receipt, ensure_ascii=False), "SECONDS", round(monotonic() - start, 2), flush=True
            )
            write(directory / "receipt.json", receipt)
            task_id, thread_id = receipt["task_id"], receipt["thread_id"]
            while True:
                job = (await client.get(f"/api/jobs/{task_id}", params={"thread_id": thread_id})).json()
                if job["status"] != "working":
                    break
                if monotonic() - start > 600:
                    job = (await client.post(f"/api/jobs/{task_id}/cancel", params={"thread_id": thread_id})).json()
                    break
                await asyncio.sleep(job.get("poll_after_seconds") or 3)
            history = (
                (await client.get("/api/runs/" + job["execution_run_id"] + "/history")).json()
                if job.get("execution_run_id")
                else {}
            )
            write(directory / "job.json", job)
            write(directory / "history.json", history)
            checks = {
                "immediate_ack": receipt["status"] == "working",
                "completed": job["status"] == "completed",
                "has_answer": bool(job.get("result", {}).get("answer")),
                "full_answer": not job.get("result", {}).get("partial", True),
                "distinct_agent_instances": len({t["call_id"] for t in history.get("subtasks", [])}) >= 2,
                "no_unplanned_delegation": len(history.get("subtasks", []))
                == len(
                    next(
                        (
                            e["data"].get("tasks", [])
                            for e in history.get("events", [])
                            if e["event"] == "research_plan"
                        ),
                        [],
                    )
                ),
                "all_subtasks_checked": all(t["check_status"] == "passed" for t in history.get("subtasks", [])),
                "final_checked": bool(
                    [e for e in history.get("events", []) if e["event"] == "answer_validation"]
                    and [e for e in history.get("events", []) if e["event"] == "answer_validation"][-1]["status"]
                    == "passed"
                ),
            }
            result = {
                "directory": str(directory),
                "seconds": round(monotonic() - start, 2),
                "checks": checks,
                "job_status": job["status"],
                "error": job.get("error"),
                "subtasks": [
                    {k: t[k] for k in ("call_id", "status", "check_status", "error")}
                    for t in history.get("subtasks", [])
                ],
            }
            write(directory / "summary.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

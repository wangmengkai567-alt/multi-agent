"""Run only original business question 1, once, against real configured providers.

Creates isolated authenticated databases; never touches production history,
never starts question 2 and never retries the business question automatically.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
QUESTION = "请查看 Deep Agents、Open Deep Research 和 smolagents 的源码，分析它们如何进行子 agent 协作与工具调用，并结合我的项目提出改进建议。关键结论需要提供源码依据。"


def save(path, value):
    text = json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value
    path.write_bytes(text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n").encode("utf-8"))


async def main(directory):
    import httpx
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    directory = directory.resolve()
    directory.relative_to((ROOT / "output").resolve())
    directory.mkdir(parents=True, exist_ok=False)
    # Isolated test account; the actual AuthService, cookie, Origin, CSRF and
    # resource ownership checks remain in use. No secret is written to reports.
    password = secrets.token_urlsafe(48)
    os.environ.update(
        APP_ACCESS_PASSWORD=password,
        APP_ORIGIN="http://127.0.0.1:8000",
        APP_BIND_HOST="127.0.0.1",
        APP_BIND_PORT="8000",
    )
    import agent.main_agent as runtime
    import api.server as server
    from api.migrate_security import migrate
    from api.security import DataLease

    runtime.checkpoint_db_path = directory / "checkpoints.db"
    runtime.project_root_path = directory
    server.run_db_path = directory / "runs.db"
    server.output_dir = directory / "files"
    server.updated_dir = directory / "updated"
    lease = DataLease(directory)
    try:
        await migrate(directory, directory)
    finally:
        lease.close()
    started = monotonic()
    async with server.lifespan(server.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1:8000"
        ) as client:
            response = await client.post(
                "/api/auth/login",
                json={"password": password},
                headers={"Origin": "http://127.0.0.1:8000", "X-Login-Intent": "login"},
            )
            response.raise_for_status()
            client.headers.update({"Origin": "http://127.0.0.1:8000", "X-CSRF-Token": response.json()["csrf_token"]})
            response = await client.post("/api/task", json={"query": QUESTION, "required_sources": ["search"]})
            response.raise_for_status()
            run_id = response.json()["run_id"]
            print("question_1_started run_id=" + run_id, flush=True)
            while True:
                run = (await client.get("/api/runs/" + run_id)).json()
                if run["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(0.5)
            execution = server.app.state.run_tasks.get(run_id)
            if execution is not None:
                await asyncio.gather(execution, return_exceptions=True)
            history = (await client.get(f"/api/runs/{run_id}/history")).json()
            snapshots = []
            work = directory / "files" / f"session_{run['thread_id']}" / f"run_{run_id}"
            for path in (work / "results").glob("*.json"):
                snapshots.append(json.loads(path.read_text(encoding="utf-8")))
            download = await client.get(f"/api/runs/{run_id}/files/download?path=answer.md")
            tools = [item for task in snapshots for item in task["evidence"]]
            source_urls = {source["url"] for row in tools for source in row["sources"] if source["kind"] == "web"}
            projects = ("langchain-ai/deepagents", "langchain-ai/open_deep_research", "huggingface/smolagents")
            project_sources = {
                project: sorted(
                    url
                    for url in source_urls
                    if "/" + project + "/" in url and ("/blob/" in url or "raw.githubusercontent.com/" in url)
                )
                for project in projects
            }
            checks = dict(
                code_sources_for_all_projects=all(project_sources.values()),
                execution_completed=run["status"] == "completed",
                business_complete=(run.get("completion") or {}).get("status") == "complete",
                actual_delegation=bool(history["subtasks"]),
                only_search=bool(history["subtasks"]) and all(row["target"] == "search" for row in history["subtasks"]),
                all_tasks_completed=bool(history["subtasks"])
                and all(row["status"] == "completed" for row in history["subtasks"]),
                actual_search=any(
                    row["name"] == "internet_search" and row["executed"] and row["status"] == "completed"
                    for row in tools
                ),
                actual_sources=any(row["sources"] for row in tools if row["executed"] and row["status"] == "completed"),
                exact_markdown=download.status_code == 200
                and download.content == (run.get("answer") or "").encode("utf-8"),
            )
            record = dict(
                question=QUESTION,
                project_sources=project_sources,
                seconds=round(monotonic() - started, 2),
                checks=checks,
                passed=all(checks.values()),
                run=run,
                subtasks=history["subtasks"],
                evidence=snapshots,
                events=history["events"],
            )
            save(directory / "question-1.json", record)
            if run.get("answer"):
                save(directory / "question-1.md", run["answer"])
            print(
                json.dumps(
                    dict(
                        passed=record["passed"],
                        checks=checks,
                        status=run["status"],
                        seconds=record["seconds"],
                        report=str(directory / "question-1.json"),
                    ),
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 0 if record["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().report_dir)))

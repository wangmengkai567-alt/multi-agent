"""Opt-in real-service acceptance. Never imported by the offline pytest suite.

Run --serve, then --smoke. Touch output/step14-live/stop to shut down gracefully.
Only storage paths change: model, tools, policies and runtime use production code.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
LIVE = ROOT / "output" / "step14-live"
PORT = 8765


def save_log(name, text):
    target = ROOT / "docs" / "implementation" / name
    target.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


async def serve():
    import uvicorn

    from agent import main_agent
    from api import server

    LIVE.mkdir(parents=True, exist_ok=True)
    stop = LIVE / "stop"
    if stop.exists():
        stop.unlink()
    main_agent.checkpoint_db_path = LIVE / "checkpoints.db"
    server.run_db_path = LIVE / "runs.db"
    server.output_dir = LIVE / "files"
    server.updated_dir = LIVE / "uploads"
    instance = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning"))

    async def watch():
        while not stop.exists():
            await asyncio.sleep(0.25)
        instance.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await instance.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    print("ACCEPTANCE_SERVER_STOPPED: graceful lifespan cleanup completed", flush=True)


async def smoke(case=None):
    import httpx
    from dotenv import dotenv_values

    from agent.runtime.events import redact_text

    env = dotenv_values(ROOT / ".env")
    lines = [
        "Real production-model acceptance: " + datetime.now(UTC).isoformat(),
        "Model: " + str(env.get("LLM_QWEN_MAX")),
        "Storage: output/step14-live (isolated)",
        "MySQL independent read-only credentials present: "
        + str(bool(env.get("MYSQL_READONLY_USER") and env.get("MYSQL_READONLY_PASSWORD"))),
    ]
    cases = [
        ("ordinary", {"query": "这是功能验收，请只回答：普通查询联调通过。不要调用工具、助手，不要生成文件。"}),
        (
            "explicit_skill",
            {
                "query": "这是报告写作Skill验收。请按照已加载的报告写作说明，将这个明确标注为测试数据的事实写成一个简短Markdown报告：测试订单为10笔。不得声称真实经营数据，不查询外部信息。请使用generate_markdown生成acceptance.md，正文注明测试数据，并在最终回答中说明报告结构。",
                "skill_ids": ["report-writing"],
            },
        ),
        (
            "real_search",
            {
                "query": "最小普通搜索验收：调用search助手，将以下范围原样写入description：用internet_search检索site:docs.tavily.com search，max_results=2，include_raw_content=false；只说明Tavily文档的一个检索功能，引用工具真实返回的Tavily官方URL，摘要一句话即可。另按系统约定保留查询对象、时间范围、统计口径三行，不将这些字段或URL计入摘要字数。若首批无官方来源，允许最多一次补充查询。不要查数据库或知识库，不生成文件。",
                "required_sources": ["search"],
            },
        ),
        (
            "internal_external",
            {
                "query": "最小内外对照链路验收：先通过db助手查询orders表订单数量；数据库不可用则记录原因并明确降级。然后请search助手仅验证一个外部事实：Shopify官方帮助文档如何定义订单状态。请将该范围原样传给search：用internet_search检索site:help.shopify.com order status，max_results=1，include_raw_content=false；引用工具真实返回的一个官方URL，摘要说明一种订单状态即可。另按系统约定附查询对象、时间范围、统计口径三行，未知写未提供。只验证这一条文档事实，不研究行业趋势、全国零售额、平台排名或任何增长率，不生成报告文件，不编造内部订单数量。",
                "skill_ids": ["ecommerce-analysis"],
                "business_mode": "internal_external",
            },
        ),
    ]
    outcomes = []
    log_name = "14-live-" + case.replace("_", "-") + ".log" if case else "14-live.log"
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=20) as client:
        for label, payload in cases:
            if case and case != label:
                continue
            print("LIVE_CASE_START: " + label, flush=True)
            response = await client.post("/api/task", json=payload)
            lines.append(f"\nCASE {label}: POST /api/task HTTP {response.status_code}")
            response.raise_for_status()
            ids = response.json()
            lines.append(json.dumps(ids, ensure_ascii=False))
            for _ in range(330):
                record = (await client.get("/api/runs/" + ids["run_id"])).json()
                if record["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(1)
            history = (await client.get("/api/runs/" + ids["run_id"] + "/history")).json()
            exported = dict(
                history,
                results=[
                    dict(
                        row,
                        raw_text="[OMITTED: raw evidence retained in isolated runtime storage; see raw_sha256/raw_path]",
                    )
                    if row.get("raw_text") is not None
                    else row
                    for row in history["results"]
                ],
            )
            lines.append(json.dumps(exported, ensure_ascii=False, indent=2))
            events = history["events"]
            checks = {"completed": history["run"]["status"] == "completed"}
            if label == "explicit_skill":
                checks["skill_body_loaded"] = any(
                    e["event"] == "skill_state"
                    and e["status"] == "loaded"
                    and e["data"].get("skill_id") == "report-writing"
                    for e in events
                )
                path = f"session_{ids['thread_id']}/run_{ids['run_id']}"
                files = (await client.get("/api/files", params={"path": path})).json().get("files", [])
                reports = [f for f in files if f["name"] == "acceptance.md"]
                checks["report_download"] = bool(reports)
                if reports:
                    download = await client.get("/api/download", params={"path": reports[0]["path"]})
                    checks["report_download"] = download.status_code == 200 and "测试" in download.text
                    raw = download.content
                    checks["report_crlf"] = b"\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")
            if label in ("real_search", "internal_external"):
                checks["search_tool_executed"] = any(
                    e["event"] == "after_tool"
                    and e["actor"].endswith(":internet_search")
                    and e["status"] == "completed"
                    and e["data"].get("executed")
                    for e in events
                )
                checks["search_review_passed"] = any(
                    s["target"] == "search" and s["check_status"] == "passed" for s in history["subtasks"]
                )
            if label == "internal_external":
                checks["handoff_recorded"] = any(e["event"] == "result_handoff" for e in events)
                if not (env.get("MYSQL_READONLY_USER") and env.get("MYSQL_READONLY_PASSWORD")):
                    checks["explicit_degradation"] = "【降级分析】" in (history["run"]["answer"] or "")
            outcomes.append(all(checks.values()))
            lines.append("ASSERTIONS " + label + ": " + json.dumps(checks, ensure_ascii=False))
            print("LIVE_CASE_END: " + label + " " + record["status"], flush=True)
            save_log(log_name, redact_text("\n".join(lines) + "\n"))
        # Confirm the exact production page/scripts are served by this app.
        for url in ("/", "/static/app.js", "/static/run-events.js"):
            response = await client.get(url)
            lines.append(f"GET {url}: HTTP {response.status_code}, bytes={len(response.content)}")
            response.raise_for_status()
    save_log(log_name, redact_text("\n".join(lines) + "\n"))
    if not all(outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--case", choices=("ordinary", "explicit_skill", "real_search", "internal_external"))
    args = parser.parse_args()
    if args.serve == args.smoke:
        parser.error("Choose exactly one of --serve or --smoke")
    asyncio.run(serve() if args.serve else smoke(args.case))

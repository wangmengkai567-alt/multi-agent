"""User-approved three-phase live acceptance through isolated authenticated HTTP routes."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

QUESTIONS = {
    1: "请研究以下三个外部开源项目：LangChain 的 Deep Agents、Open Deep Research，以及 Hugging Face 的 smolagents。"
    "比较它们如何委派子任务、执行工具、返回结果，以及处理失败与取消。结合上传的《项目代码摘录.md》，指出本项目的具体问题和可借鉴实现。"
    "关键结论附实际源码链接及符号或行位置，区分已确认事实和待核实疑点，明确代码摘录不能代表本项目全部实现。生成 Markdown 报告。",
    2: "不再联网，只依据上一轮已核实的结论，整理修复实施计划，包含修改位置、依赖关系、实施顺序、验收标准和未解决问题。"
    "不要增加新事实，生成 PDF。缺少上一轮已审核材料时，请明确要求补充，不能用记忆或其他会话内容补齐。",
    3: "请核对上传的销售明细、退款明细和经营总结中的销售额、退款额和增长率，说明统计时间、退款归属和计算口径。"
    "经营总结中的数字可能与明细不一致，请定位差异并列出需要人工确认的问题。禁止联网，生成 Markdown 核对报告。"
    "这些材料均为模拟测试数据，不代表真实经营事实；对重复编号和跨期退款不要自行决定是否剔除或计入。",
}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    path.write_bytes((text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n") + "\r\n").encode("utf-8"))


def materials(directory, phase):
    if phase == 1:
        selections = [
            ("agent/main_agent.py", 62, 88),
            ("agent/subagent_factory.py", 95, 119),
            ("agent/runtime/delegation.py", 40, 68),
            ("agent/subagents/definitions.py", 29, 41),
        ]
        parts = ["# 项目代码摘录\n仅为当前工作区若干真实片段；不能据此声称审计了整个项目。\n"]
        manifest = []
        for name, first, last in selections:
            raw = (ROOT / name).read_bytes()
            lines = raw.decode("utf-8-sig").splitlines()
            fragment = "\n".join(f"{number}: {lines[number - 1]}" for number in range(first, min(last, len(lines)) + 1))
            parts.append(f"## {name}，原文件行 {first}–{last}\n```python\n{fragment}\n```\n")
            manifest.append(dict(path=name, first=first, last=last, sha256=hashlib.sha256(raw).hexdigest()))
        save(directory / "项目代码摘录.md", "\n".join(parts))
        save(directory / "source-manifest.json", manifest)
        return [directory / "项目代码摘录.md"]
    if phase != 3:
        return []
    bodies = {
        "销售明细.md": """# 模拟测试数据：销售明细
金额单位：元；下面记录仅用于系统验收。
|订单编号|销售日期|金额|说明|
|---|---|---:|---|
|A001|2026-08-01|1000|历史订单|
|A002|2026-08-02|2000|正常订单|
|B001|2026-09-01|1500|正常订单|
|B002|2026-09-02|1800|第一条记录|
|B002|2026-09-02|1800|重复编号；是否重复入账尚未确认|
""",
        "退款明细.md": """# 模拟测试数据：退款明细
金额单位：元；没有约定跨期退款的财务归属规则。
|退款编号|订单编号|退款日期|金额|说明|
|---|---|---|---:|---|
|R001|A001|2026-09-03|200|退款发生于9月、原订单属于8月|
|R002|B001|2026-09-04|300|原订单及退款均属于9月|
""",
        "经营总结.md": """# 模拟测试数据：经营总结
8月销售额3000元。
9月销售额3300元，较8月增长20%。
9月退款额只有300元，所以9月净销售额为3000元。
本总结没有说明重复订单是否剔除，也没有说明按退款发生月还是订单月份归属。
以上均是待核对的材料记载，不能直接当作审计结论。
""",
    }
    for name, body in bodies.items():
        save(directory / name, body)
    save(
        directory / "expected.json",
        {
            "august_gross": 3000,
            "september_gross_if_duplicate_removed": 3300,
            "growth_if_duplicate_removed": "10%",
            "september_gross_if_all_rows_counted": 5100,
            "growth_if_all_rows_counted": "70%",
            "refunds_by_occurrence_month": 500,
            "refunds_for_september_orders": 300,
            "net_if_deduplicated_and_occurrence_month": 2800,
            "net_if_deduplicated_and_order_month": 3000,
            "unresolved": ["B002是否真实重复入账", "跨期退款归属口径"],
        },
    )
    return [directory / name for name in bodies]


async def api(client, method, path, *, csrf=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if method != "GET":
        headers["Origin"] = "http://127.0.0.1:8000"
        if csrf:
            headers["X-CSRF-Token"] = csrf
    response = await client.request(method, path, headers=headers, **kwargs)
    if not response.is_success:
        from agent.runtime.events import redact_text

        raise RuntimeError(f"HTTP {response.status_code} {method} {path}: {redact_text(response.text)[:1600]}")
    return response


async def run_case(client, server, directory, number, thread, csrf, files=(), *, control=False):
    case_dir = directory / f"phase-{number}" / ("new-thread" if control else "main")
    case_dir.mkdir(parents=True, exist_ok=True)
    if files:
        response = await api(
            client,
            "POST",
            "/api/upload",
            csrf=csrf,
            data={"thread_id": thread},
            files=[("files", (file.name, file.read_bytes(), "text/markdown; charset=utf-8")) for file in files],
        )
        save(case_dir / "upload.json", response.json())
    selected = {
        1: ["research", "document-analysis", "report-writing"],
        2: ["report-writing"],
        3: ["document-analysis", "data-analysis", "report-writing"],
    }[number]
    body = dict(
        query=QUESTIONS[number],
        thread_id=thread,
        execution_mode="foreground",
        skill_ids=selected,
        network_policy="allowed" if number == 1 else "forbidden",
        required_uploads=[f.name for f in files],
    )
    if number == 1:
        body["required_sources"] = ["search"]
    save(case_dir / "request.json", body)
    started = monotonic()
    receipt = (await api(client, "POST", "/api/task", csrf=csrf, json=body)).json()
    save(case_dir / "receipt.json", receipt)
    run_id = receipt["run_id"]
    print(f"START phase={number} control={control} run_id={run_id}", flush=True)
    task = server.app.state.run_tasks.get(run_id)
    last_progress = -30
    while True:
        row = (await api(client, "GET", f"/api/runs/{run_id}")).json()
        elapsed = monotonic() - started
        if row["status"] not in {"queued", "running"}:
            break
        if elapsed - last_progress >= 20:
            events = (await api(client, "GET", f"/api/runs/{run_id}/events", params={"limit": 1000})).json()["events"]
            last = events[-1] if events else {}
            print(
                f"PROGRESS elapsed={round(elapsed)} status={row['status']} events={len(events)} last={last.get('event')} actor={last.get('actor')}",
                flush=True,
            )
            last_progress = elapsed
        if task is not None and task.done():
            raise RuntimeError("Execution ended without a terminal database record")
        if elapsed > server.app.state.runtime_policy.timeouts.total_seconds + 30:
            await api(client, "POST", f"/api/runs/{run_id}/cancel", csrf=csrf)
            raise RuntimeError("Application execution exceeded its configured deadline")
        await asyncio.sleep(1)
    await server.app.state.event_sink.flush(run_id)
    history = (await api(client, "GET", f"/api/runs/{run_id}/history")).json()
    listing = (await api(client, "GET", f"/api/runs/{run_id}/files")).json()
    save(case_dir / "history.json", history)
    save(case_dir / "files.json", listing)
    save(case_dir / "answer.md", row.get("answer") or "未获得最终交付")
    # Downloads go through the same authorized HTTP interface used by the page.
    downloads = []
    for file in listing["files"]:
        if file["relative_path"] not in {"answer.md", "answer.pdf", "task-contract.json", "delivery-artifact.json"}:
            continue
        response = await api(client, "GET", file["url"])
        target = case_dir / "downloads" / file["name"]
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(response.content)
        downloads.append(
            dict(
                path=file["relative_path"],
                bytes=len(response.content),
                sha256=hashlib.sha256(response.content).hexdigest(),
            )
        )
    jobs = []
    if number == 2 and not control:
        deadline = monotonic() + 120
        while True:
            jobs = (await api(client, "GET", f"/api/runs/{run_id}/jobs")).json()["jobs"]
            if not any(job["status"] == "working" for job in jobs) or monotonic() > deadline:
                break
            await asyncio.sleep(2)
        save(case_dir / "jobs.json", jobs)
        for job in jobs:
            for artifact in (job.get("result") or {}).get("artifacts", []):
                response = await api(client, "GET", artifact["url"])
                target = case_dir / "downloads" / artifact["name"]
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(response.content)
                downloads.append(
                    dict(
                        path=artifact["relative_path"],
                        bytes=len(response.content),
                        sha256=hashlib.sha256(response.content).hexdigest(),
                    )
                )
    events = history["events"]
    calls = [e for e in events if e["event"] == "before_tool"]
    network = [
        e
        for e in calls
        if e["actor"].endswith((":research_step", ":internet_search", ":read_web_page", ":submit_research_job"))
    ]
    reviews = [e for e in events if e["event"] == "answer_validation"]
    contract_file = case_dir / "downloads" / "task-contract.json"
    contract = json.loads(contract_file.read_text("utf-8")) if contract_file.exists() else None
    checks = {
        "terminal_completed": row["status"] == "completed",
        "final_review_passed": bool(reviews and reviews[-1]["status"] == "passed"),
        "complete_coverage": row.get("coverage") == "complete",
        "downloaded_markdown": any(d["path"] == "answer.md" and d["bytes"] > 0 for d in downloads),
    }
    if number in {2, 3}:
        checks["zero_network_tool_calls"] = len(network) == 0
    if control:
        checks = {
            "zero_network_tool_calls": not network,
            "no_previous_material_bound": bool(
                contract and not any(m["kind"] == "previous" for m in contract["materials"])
            ),
        }
    if control:
        checks["clarification_requested"] = any(event["event"] == "clarification_requested" for event in events)
        checks["no_file_conversion"] = not any(event["actor"].endswith(":convert_md_to_pdf") for event in calls)
    if number == 1:
        checks["actual_search_execution"] = bool(network)
        checks["document_read_execution"] = any(e["actor"].endswith(":read_document") for e in calls)
    if number == 3:
        checks["document_read_execution"] = any(e["actor"].endswith(":read_document") for e in calls)
    if number == 2 and not control:
        original = json.loads((directory / "phase-1/main/receipt.json").read_text("utf-8"))
        checks["bound_expected_previous_run"] = bool(
            contract
            and any(
                material["kind"] == "previous" and material["reference"] == original["run_id"]
                for material in contract["materials"]
            )
        )
        checks["downloaded_pdf"] = any(d["path"] == "answer.pdf" and d["bytes"] > 0 for d in downloads)
    result = dict(
        phase=number,
        control=control,
        question=QUESTIONS[number],
        run_id=run_id,
        thread_id=thread,
        status=row["status"],
        coverage=row.get("coverage"),
        error=row.get("error"),
        elapsed_seconds=round(monotonic() - started, 2),
        checks=checks,
        network_tool_calls=len(network),
        tool_counts=dict(Counter(e["actor"] for e in calls)),
        subtasks=[
            {k: t[k] for k in ("call_id", "target", "status", "check_status", "error")} for t in history["subtasks"]
        ],
        results=[
            {
                k: r.get(k)
                for k in ("result_id", "source", "check_status", "facts_status", "coverage_status", "published")
            }
            for r in history["results"]
        ],
        final_reviews=reviews,
        contract=contract,
        downloads=downloads,
        jobs=jobs,
        semantic_verdict="pending independent examination of evidence and answer",
    )
    save(case_dir / "result.json", result)
    print(
        f"FINISH phase={number} control={control} status={row['status']} coverage={row.get('coverage')} checks={json.dumps(checks)}",
        flush=True,
    )
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, type=int, choices=(1, 2, 3))
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    directory = (
        args.directory.resolve()
        if args.directory
        else ROOT / "output/project-acceptance" / datetime.now().strftime("live-%Y%m%d-%H%M%S")
    )
    directory.relative_to(ROOT / "output/project-acceptance")
    if args.phase != 1 and args.directory is None:
        raise ValueError("Later phases require the same acceptance directory")
    if args.phase == 1:
        directory.mkdir(parents=True, exist_ok=False)
    os.environ["APP_ACCESS_PASSWORD"] = secrets.token_urlsafe(48)
    os.environ["APP_ORIGIN"] = "http://127.0.0.1:8000"
    os.environ["APP_BIND_HOST"] = "127.0.0.1"
    os.environ["APP_BIND_PORT"] = "8000"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    import httpx

    import agent.main_agent as runtime
    import api.server as server
    from api.migrate_security import migrate
    from api.security import DataLease

    isolated = directory / "instance"
    data = isolated / "data"
    data.mkdir(parents=True, exist_ok=True)
    if args.phase == 1:
        lease = DataLease(data)
        try:
            await migrate(data, isolated)
        finally:
            lease.close()
    runtime.checkpoint_db_path = data / "checkpoints.db"
    runtime.project_root_path = isolated
    server.run_db_path = data / "runs.db"
    server.output_dir = isolated / "output"
    server.updated_dir = isolated / "updated"
    fixture_dir = directory / f"phase-{args.phase}" / "materials"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    files = materials(fixture_dir, args.phase)
    print(f"EVIDENCE_DIRECTORY={directory}", flush=True)
    async with server.lifespan(server.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1:8000"
        ) as client:
            response = await api(
                client,
                "POST",
                "/api/auth/login",
                headers={"X-Login-Intent": "login"},
                json={"password": os.environ["APP_ACCESS_PASSWORD"]},
            )
            csrf = response.json()["csrf_token"]
            save(directory / f"phase-{args.phase}" / "catalog.json", (await api(client, "GET", "/api/skills")).json())
            if args.phase == 2:
                first = json.loads((directory / "phase-1/main/receipt.json").read_text("utf-8"))
                thread = first["thread_id"]
            else:
                thread = (await api(client, "POST", "/api/threads", csrf=csrf)).json()["thread_id"]
            result = await run_case(client, server, directory, args.phase, thread, csrf, files)
            if args.phase == 2:
                new_thread = (await api(client, "POST", "/api/threads", csrf=csrf)).json()["thread_id"]
                await run_case(client, server, directory, args.phase, new_thread, csrf, control=True)
            await api(client, "POST", "/api/auth/logout", csrf=csrf)
    return 0 if all(result["checks"].values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        from agent.runtime.events import redact_text

        print(f"ACCEPTANCE_ERROR={type(error).__name__}: {redact_text(str(error))[:2000]}", flush=True)
        raise SystemExit(2) from None

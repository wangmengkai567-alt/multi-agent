"""Track actual upload reads and deliver exactly the final user-visible answer."""

import json
from hashlib import sha256

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.runtime.context import get_run_context
from agent.runtime.events import redact_text
from agent.runtime.output import cited_urls, source_page
from utils.secure_paths import confined_path, resolve_run_path


def _range_covered(extent, ranges):
    """Check a rectangle union by row boundaries, without enumerating empty cells."""
    top, bottom, left, right = extent
    if bottom < top or right < left:
        return True
    clipped = [
        (max(top, a), min(bottom, b), max(left, c), min(right, d))
        for a, b, c, d in ranges
        if a <= bottom and b >= top and c <= right and d >= left
    ]
    boundaries = sorted({top, bottom + 1, *(r for a, b, _, _ in clipped for r in (a, b + 1))})
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        cursor = left
        for col_start, col_end in sorted((c, d) for a, b, c, d in clipped if a <= start and b >= end - 1):
            if col_start > cursor:
                return False
            cursor = max(cursor, col_end + 1)
        if cursor <= right:
            return False
    return True


def _document_read_complete(context, file, digest, artifact):
    from agent.runtime.evidence_protocol import DocumentArtifact, validate_input

    data = DocumentArtifact.model_validate(artifact)
    source = validate_input(data.document, context)
    if source.path != file.relative_to(context.work_dir.resolve()).as_posix() or source.sha256 != digest:
        raise ValueError("Document read does not match the current upload")
    coverage = data.coverage
    if coverage.scope == "catalog":
        return False
    if not coverage.extents:
        return coverage.complete and not coverage.missing
    with context.budget_lock:
        key = str(file)
        state = context.document_coverage.get(key)
        if state is None or state["sha256"] != digest:
            state = dict(sha256=digest, parser=source.parser, extents=coverage.extents, ranges={}, missing=[])
            context.document_coverage[key] = state
        if state["extents"] != coverage.extents or state["parser"] != source.parser:
            raise ValueError("Document coverage metadata changed for the same input snapshot")
        state["missing"] = sorted(set(state["missing"]) | set(coverage.missing))
        for target, bounds in coverage.read_ranges.items():
            ranges = state["ranges"].setdefault(target, [])
            if bounds not in ranges:
                ranges.append(bounds)
        return not state["missing"] and all(
            _range_covered(extent, state["ranges"].get(target, [])) for target, extent in state["extents"].items()
        )


def after_tool_workflow(call):
    context = get_run_context()
    if context is None or not call.executed or call.status != "completed":
        return
    messages = [call.result] if isinstance(call.result, ToolMessage) else []
    if isinstance(call.result, Command) and isinstance(call.result.update, dict):
        messages = call.result.update.get("messages", [])
    if isinstance(messages, ToolMessage):
        messages = [messages]
    if not any(isinstance(m, ToolMessage) and m.content and not str(m.content).startswith("Error") for m in messages):
        return
    name = call.actor.rsplit(":", 1)[-1]
    if name in ("read_file", "read_file_content", "read_document"):
        value = call.args.get("file_path" if name == "read_file" else "filename")
        file = resolve_run_path(value, virtual=name == "read_file")
        if not file.is_relative_to((context.work_dir / "uploads").resolve()) or not file.is_file():
            return
        raw = file.read_bytes()
        digest = sha256(raw).hexdigest()
        if name in ("read_document", "read_file_content"):
            if name == "read_document":
                artifacts = [m.artifact for m in messages if isinstance(m, ToolMessage) and m.artifact is not None]
                if len(artifacts) != 1:
                    return
                complete = _document_read_complete(context, file, digest, artifacts[0])
                if not complete:
                    return
            elif file.suffix.lower() in (".docx", ".pdf", ".xlsx", ".xls"):
                return  # Inspection is not the document expert's located evidence.
        if name == "read_file":
            try:
                total = len(raw.decode("utf-8").splitlines())
            except UnicodeError:
                return
            offset, limit = call.args.get("offset", 0), call.args.get("limit", 2000)
            with context.budget_lock:
                coverage = context.upload_coverage.setdefault((str(file), digest), set())
                coverage.update(range(max(0, offset), min(total, offset + limit)))
                if len(coverage) != total or not total:
                    return
        with context.budget_lock:
            context.upload_reads[str(file)] = digest


def required_sources(context):
    return set(context.required_sources) | ({"db", "search"} if context.business_mode == "internal_external" else set())


async def finalize_answer(context, answer):
    from agent.runtime.completion import validate_delivery
    from agent.runtime.contracts import assert_contract

    context.check_active()
    assert_contract(context)
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Final answer must not be empty")
    rows = list(context.task_results.values())
    successes = [row for row in rows if row["status"] == "completed"]
    failures = [row for row in rows if row["status"] != "completed"]
    obtained = {context.expert_contract[row["target"]]["execution"]["source_kind"] for row in successes}
    missing = sorted(required_sources(context) - obtained)
    unread = [
        name
        for name in context.required_uploads
        if str(confined_path("uploads/" + name, context.work_dir)) not in context.upload_reads
    ]
    if (rows or context.task_rejections or missing or unread) and not successes:
        raise RuntimeError("本轮调查未取得有效分工结果，不能生成调查结论或成功文件；详见来源要求和子任务错误记录")
    allowed = set(cited_urls(context.user_query))
    for row in successes:
        for source in row["sources"]:
            if source["url"]:
                allowed.add(source["url"])
            allowed.update(cited_urls(source["content"]))
    if context.input_text:
        allowed.update(cited_urls(context.input_text))
    if {source_page(url) for url in cited_urls(answer)} - {source_page(url) for url in allowed}:
        raise ValueError("最终答复包含实际输入和工具结果中不存在的来源 URL")
    notices = []
    for row in successes:
        if row["unfinished"]:
            notices.append(
                row["display_name"] + "的未完成事项：" + "；".join(item["reason"] for item in row["unfinished"])
            )
    if failures:
        notices.append(
            "未完成的分工："
            + "；".join(row["display_name"] + "（" + (row["error"] or row["status"]) + "）" for row in failures)
        )
    if context.task_rejections:
        notices.append("被拒绝的分工：" + "；".join(context.task_rejections))
    if missing:
        notices.append("尚未取得结果的必需来源：" + "、".join(missing))
    if unread:
        notices.append("尚未完整读取的必需文件：" + "、".join(unread))
    if notices:
        answer += "\n\n【部分完成】\n\n" + "\n\n".join(notices)
    limitations = [
        row["display_name"] + "的读取或执行限制：" + "；".join(item["reason"] for item in row["limitations"])
        for row in rows
        if row["limitations"]
    ]
    if limitations:
        answer += "\n\n【读取与执行限制】\n\n" + "\n\n".join(limitations)
    answer = redact_text(answer).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if not answer.strip():
        raise ValueError("Final answer must not be empty")
    raw = answer.encode("utf-8")
    context.work_dir.mkdir(parents=True, exist_ok=True)
    path = confined_path("answer.md", context.work_dir)
    temporary = confined_path(".answer.tmp", context.work_dir)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
        with context.budget_lock:
            context.check_active()
            temporary.replace(path)
            context.coverage = "partial" if notices else "complete"
            digest = sha256(raw).hexdigest()
            context.delivery_draft = dict(
                delivered_content_digest=digest,
                artifact=dict(
                    kind="markdown", path="answer.md", status="completed", content_digest=digest, file_digest=digest
                ),
            )
    finally:
        temporary.unlink(missing_ok=True)
    validate_delivery(context, answer)
    return answer


def instruction(context):
    return "\n【本轮输入与权限】" + json.dumps(
        dict(
            network_policy=context.network_policy,
            required_sources=sorted(required_sources(context)),
            required_uploads=list(context.required_uploads),
            input_text_reference="input_text" if context.input_text is not None else None,
            rule="按任务需要逐个委派；成功结果与失败分工分别说明。缺失资料不得编造。",
        ),
        ensure_ascii=False,
    )

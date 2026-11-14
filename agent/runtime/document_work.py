"""Project the next document unit from bound inputs and accepted owned records."""

from pathlib import PurePosixPath

from agent.runtime.evidence_protocol import EvidenceError

ENDED_TASKS = frozenset(("completed", "failed", "cancelled", "interrupted"))


def _records(context, item, task_id):
    for owner in item["task_ids"] if item else ():
        row = context.task_results[owner]
        if row["status"] in ENDED_TASKS:
            for record in row["evidence"]:
                yield record, row["sources"]
    if task_id is not None:
        from agent.reflection import tool_evidence
        from agent.runtime.hooks import active_call

        call = active_call.get()
        row = context.task_results[task_id]
        if item is None and row["status"] in ENDED_TASKS:
            for record in row["evidence"]:
                yield record, row["sources"]
        elif call and call.task_id == task_id and row["status"] not in ENDED_TASKS:
            for record in tool_evidence.get() or ():
                yield record, record["sources"]


def _unread(total, ranges):
    cursor, unread = 1, []
    for start, end in sorted(ranges):
        if start > cursor:
            unread.append([cursor, start - 1])
        cursor = max(cursor, end + 1)
    if cursor <= total:
        unread.append([cursor, total])
    return unread


def document_progress(context, item=None, *, task_id=None):
    """Internal records are already normalized; catalogs never add read ranges or sources."""
    from tools.document_vision import MAX_PAGES, VISUAL_SUFFIXES

    contract = context.task_contract
    states = (
        {
            material.reference: dict(
                reference=material.reference,
                digest=material.digest,
                visual=PurePosixPath(material.reference).suffix.lower() in VISUAL_SUFFIXES,
                paginated=PurePosixPath(material.reference).suffix.lower() == ".pdf",
                pages=None,
                extents={},
                complete=False,
                max_pages_per_read=MAX_PAGES,
                read_ranges=[],
                unread_ranges=[],
                missing=[],
                sources=[],
                catalog_records=[],
            )
            for material in contract.materials
            if material.kind == "upload"
        }
        if contract
        else {}
    )
    for record, accepted in _records(context, item, task_id):
        if record["adapter"] != "document.v1" or record["status"] != "completed" or not record["executed"]:
            continue
        artifact = record["artifact"]
        document, coverage = artifact["document"], artifact["coverage"]
        state = states[document["path"]]
        state["extents"] = coverage["extents"]
        state["complete"] |= coverage["complete"]
        for metadata in coverage["catalog"]:
            if "pages" in metadata and "max_pages_per_read" in metadata:
                state["pages"] = metadata["pages"]
                state["max_pages_per_read"] = metadata["max_pages_per_read"]
        if coverage["scope"] == "catalog":
            state["catalog_records"].append(dict(task_id=record["task_id"], call_id=record["call_id"]))
            continue
        if "pages" in coverage["read_ranges"]:
            state["read_ranges"].append(coverage["read_ranges"]["pages"][:2])
        state["missing"] = sorted(set(state["missing"]) | set(coverage["missing"]))
        state["sources"].extend(
            dict(
                task_id=record["task_id"],
                call_id=record["call_id"],
                source_key=source["key"],
                locator=source["locator"],
            )
            for source in record["sources"]
            if source in accepted
        )
    for state in states.values():
        if state["pages"] is not None:
            state["read_ranges"] = [list(bounds) for bounds in sorted(set(map(tuple, state["read_ranges"])))]
            state["unread_ranges"] = _unread(state["pages"], state["read_ranges"])
            state["complete"] = not state["unread_ranges"] and not state["missing"]
        if state["paginated"] and state["pages"] is None:
            state["next_selection"] = dict(mode="catalog")
        elif state["visual"] and state["pages"] is not None:
            ranges = state["unread_ranges"]
            state["next_selection"] = (
                dict(
                    mode="content",
                    page_start=ranges[0][0],
                    page_end=min(ranges[0][1], ranges[0][0] + state["max_pages_per_read"] - 1),
                )
                if ranges
                else None
            )
        else:
            state["next_selection"] = None if state["complete"] else dict(mode="content")
    return list(states.values())


def unread_bound_text(context, item, task_id=None):
    contract = context.task_contract
    remaining = (
        {material.reference for material in contract.materials if material.text is not None} if contract else set()
    )
    for record, _ in _records(context, item, task_id):
        if record["name"] == "read_bound_material" and record["status"] == "completed" and record["executed"]:
            remaining.discard(record["request_parameters"]["reference"])
    return sorted(remaining)


def execution_unit(context, item, spec=None):
    """Unknown targets use requirement kinds; selected experts use their actual tools."""
    names = {tool.name for tool in spec.tools} if spec is not None else None
    conditions = item["completion_conditions"] if item else ()
    document = (
        "read_document" in names
        if names is not None
        else any(condition["evidence_kind"] == "document" for condition in conditions)
    )
    tools, nested, kind, documents = 1, 0, "native_or_query", []
    if document:
        documents = document_progress(context, item)
        pending = [state for state in documents if state["next_selection"] is not None]
        pdf = next((state for state in pending if state["paginated"] and state["pages"] is None), None)
        visual = next((state for state in pending if state["visual"]), None)
        if pdf is not None:
            tools, nested, kind = 2, 1, "pdf_catalog_and_content"
        elif visual is not None:
            tools, nested, kind = 1, 1, "visual_content"
        elif documents and not pending and not unread_bound_text(context, item):
            tools, kind = 0, "document_no_unread_range"
    elif any(c["evidence_kind"] == "web" and c["page_required"] for c in conditions) and (
        names is None or {"internet_search", "read_web_page"} <= names
    ):
        tools, kind = 2, "search_and_page"
    seconds = context.policy.stages.expert_cost(context.policy.timeouts.shutdown_seconds, tools, nested)
    return dict(
        kind=kind,
        tool_calls=tools,
        thinking_model_calls=tools,
        internal_model_calls=nested,
        handoff_model_calls=1,
        seconds=seconds,
        documents=documents,
    )


def next_document_step(context):
    from agent.runtime.hooks import active_call

    call = active_call.get()
    if call is None or call.task_id is None:
        return None
    row = context.task_results[call.task_id]
    unit = row["stage_budget"].get("execution_unit")
    if unit is None or not unit["documents"]:
        return None
    item = context.required_work[row["work_item_id"]] if row["work_item_id"] else None
    states = document_progress(context, item, task_id=call.task_id)
    pending = [state for state in states if state["next_selection"] is not None]
    return max(
        pending, key=lambda state: state["visual"] and state["next_selection"]["mode"] != "catalog", default=None
    )


def check_continuation_selection(context, reference, selection):
    """A new task of the same item may read only ranges its predecessors left unread."""
    from agent.runtime.hooks import active_call
    from agent.runtime.policies import PolicyError

    call = active_call.get()
    if call is None or call.task_id is None:
        return
    row = context.task_results[call.task_id]
    item = context.required_work.get(row["work_item_id"])
    if item is None or not any(owner != call.task_id for owner in item["task_ids"]):
        return
    state = next(
        state for state in document_progress(context, item, task_id=call.task_id) if state["reference"] == reference
    )
    if state["next_selection"] is None:
        raise PolicyError("同事项已没有该材料的未读范围；保留原证据和识别缺口，不重复读取")
    if not state["paginated"]:
        return
    selected = selection or {}
    if selected.get("mode") == "catalog":
        if state["pages"] is not None:
            raise PolicyError("同事项已有目录，后续分工仅处理未读页，不能重复读取目录")
        return
    start, end = selected.get("page_start"), selected.get("page_end")
    if start is None or end is None or not any(a <= start <= end <= b for a, b in state["unread_ranges"]):
        raise PolicyError("同事项接续必须显式选择现有未读页范围，不能重复已读页")


def previous_work_rows(context, item, current_task_id=None):
    """Cross-task handoff is an ownership boundary, unlike internal budget projection."""
    rows = []
    for task_id in item["task_ids"] if item else ():
        if task_id == current_task_id:
            continue
        row = context.task_results[task_id]
        if (
            row["call_id"] != task_id
            or row["run_id"] != context.run_id
            or row["thread_id"] != context.thread_id
            or row["work_item_id"] != item["item_id"]
            or row["status"] not in ENDED_TASKS
        ):
            raise EvidenceError("同事项交接必须属于本轮已结束的关联分工")
        rows.append(row)
    return rows


def check_work_continuation(context, item):
    from agent.runtime.policies import PolicyError

    previous = previous_work_rows(context, item)
    if any(
        row["status"] in ("cancelled", "interrupted") or row["error_category"] == "stage_cleanup" for row in previous
    ):
        raise PolicyError("取消、中断或清理失败的分工不能接续取证")
    if any(c["evidence_kind"] == "document" for c in item["completion_conditions"]):
        documents = document_progress(context, item)
        if (
            documents
            and not unread_bound_text(context, item)
            and all(state["next_selection"] is None for state in documents)
        ):
            raise PolicyError("该事项没有可接续的未读范围；保留既有证据和识别缺口，不重复整个分工")

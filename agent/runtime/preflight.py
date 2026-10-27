"""Validate selected dependencies before business inference; unavailable sources remain explicit gaps."""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from agent.runtime.errors import DependencyUnavailable, ExecutionTerminated
from agent.runtime.events import make_event, redact_text
from agent.runtime.policies import PolicyError, request_timeout
from agent.runtime.tool_timeout import bounded_tool
from agent.runtime.workers import WorkerOperationError, run_operation

PARSERS = {".docx": "docx", ".pdf": "pypdf", ".xlsx": "openpyxl", ".xls": "xlrd"}


def unavailable(context, capability):
    return context.capability_preflight.get(capability, {}).get("status") == "unavailable"


def _check(context, capability, *, required):
    with context.preflight_lock:
        saved = context.capability_preflight.get(capability)
        if saved is not None:
            return saved
        try:
            if capability == "db":
                from agent.runtime.policies import reserve_calls
                from tools.db_tools import database_operation, get_db_config

                get_db_config()  # Reject missing credentials before charging the metadata probe.
                reserve_calls("tool")
                database_operation("probe")
            elif capability == "search":
                load_dotenv()
                if not os.getenv("TAVILY_API_KEY"):
                    raise DependencyUnavailable("Tavily API key is missing")
                run_operation(
                    "dependency_probe",
                    {"modules": ["tavily"]},
                    seconds=request_timeout(context.policy.timeouts.tool_seconds),
                )
            else:
                modules = []
                if capability.startswith("document:"):
                    suffix = capability.removeprefix("document:")
                    from tools.document_parser import TEXT_SUFFIXES

                    if suffix in PARSERS:
                        modules = [PARSERS[suffix]]
                        if suffix == ".xls":
                            modules.append("openpyxl")
                    elif suffix not in TEXT_SUFFIXES:
                        raise DependencyUnavailable("Unsupported document format: " + suffix)
                run_operation(
                    "dependency_probe",
                    dict(modules=modules),
                    seconds=request_timeout(context.policy.timeouts.tool_seconds),
                )
            row = dict(status="available", required=required, reason="")
        except ExecutionTerminated:
            raise
        except (DependencyUnavailable, WorkerOperationError, PolicyError, ImportError, OSError, TimeoutError) as error:
            row = dict(status="unavailable", required=required, reason=redact_text(str(error)))
        context.check_active()
        context.capability_preflight[capability] = row
        if context.event_sink:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "capability_preflight",
                    "依赖检查：" + capability,
                    dict(capability=capability, **row),
                    status=row["status"],
                    call_id="preflight:" + capability + ":" + context.run_id,
                )
            )
        return row


def prepare_run(context):
    contract = context.task_contract
    if contract is None or contract.delivery_kind == "clarification" or context.preflight_complete:
        return
    required = set(contract.required_sources)
    if contract.business_mode == "internal_external":
        required.update(("db", "search"))
    if contract.purpose == "database":
        required.add("db")
    if contract.network_allowed:
        _check(context, "search", required="search" in required or contract.purpose == "research")
    if "db" in required:
        _check(context, "db", required=True)
    for material in contract.materials:
        if material.kind == "upload":
            _check(context, "document:" + Path(material.reference).suffix.lower(), required=True)
    context.preflight_complete = True


async def aprepare_run(context):
    if context.preflight_complete:
        return
    with bounded_tool("dependency_preflight", None):
        await asyncio.to_thread(prepare_run, context)


def ensure_expert(context, spec):
    source = spec.execution.source_kind
    if source in ("db", "search"):
        row = _check(context, source, required=source in context.task_contract.required_sources)
        if row["status"] == "unavailable":
            raise DependencyUnavailable(row["reason"])
    if any(tool.name == "read_document" for tool in spec.tools):
        rows = [
            _check(context, "document:" + Path(material.reference).suffix.lower(), required=True)
            for material in context.task_contract.materials
            if material.kind == "upload"
        ]
        if rows and all(row["status"] == "unavailable" for row in rows):
            raise DependencyUnavailable("; ".join(dict.fromkeys(row["reason"] for row in rows)))


async def aensure_expert(context, spec):
    with bounded_tool("expert_preflight", None):
        await asyncio.to_thread(ensure_expert, context, spec)

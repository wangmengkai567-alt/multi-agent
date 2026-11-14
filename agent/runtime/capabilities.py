"""Project the locked execution policy into model tools and actual admission."""

import json
from copy import deepcopy
from urllib.parse import urlsplit

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from agent.reflection import source_urls, tool_evidence
from agent.runtime.context import get_run_context
from agent.runtime.evidence_protocol import adapter_for_tool
from agent.runtime.experts import append_system
from agent.runtime.finalization import final_answer_mode, prepare_final_request
from agent.runtime.hooks import active_call
from agent.runtime.output import cited_urls
from agent.runtime.policies import PolicyError, calls_available, search_call_available
from agent.runtime.required_work import active_work_item, required_work_snapshot
from agent.runtime.stage_budget import (
    admit_stage,
    close_limited_work,
    coordination_window,
    record_decision,
    tools_admitted,
)

FILE_ACTIONS = frozenset(("write_file", "edit_file", "generate_markdown", "execute"))
NETWORK_ACTIONS = frozenset(("internet_search", "read_web_page"))
READONLY_FILESYSTEM_PROMPT = """文件工具只读查看本轮文件与 /skills/ 说明。
read_file 使用从 0 开始的 offset 和 limit 分页。grep 为字面量匹配。
glob 用 path 指定目录、pattern 指定相对模式，例如 path='/skills/'、pattern='*/SKILL.md'。
只使用本轮实际提供的工具；最终正文由程序自动保存为 Markdown。
"""


def expert_available(context, spec):
    return not (spec.execution.network_access and context.network_policy == "forbidden")


# 根据本轮联网权限、输入材料和可用专家判断工具是否可用；network 标识工具实际需要联网。
def tool_available(context, name, *, network=False, target_specs=None):
    if name in FILE_ACTIONS:
        return False
    if context is None:
        return True
    if context.stop_status is not None or context.terminal_error is not None:
        return False
    remaining = context.collection_remaining()
    if context.report_started or remaining is not None and remaining <= 0:
        return False
    if name == "task" and not context.can_delegate():
        return False
    call = active_call.get()
    if not (call and call.task_id) and coordination_window(context) <= 0:
        decision = admit_stage(context)
        record_decision(context, decision, "tool_admission")
        close_limited_work(context, decision)
        return False
    if call and call.task_id and not tools_admitted(context):
        return False
    if name == "update_required_work" and call and call.task_id:
        return False
    if name == "task" and context.required_work and active_work_item(context) is None:
        return False
    if call and call.task_id and call.actor.partition(":")[0] == "search":
        with context.budget_lock:
            if not any(search_call_available(tool, call.task_id) for tool in NETWORK_ACTIONS):
                return False
            if name not in NETWORK_ACTIONS and any(
                context.search_counts.get((call.task_id, tool), 0) for tool in NETWORK_ACTIONS
            ):
                return False
    if (network or name in NETWORK_ACTIONS) and context.network_policy == "forbidden":
        return False
    if name == "read_bound_material" and context.task_contract is not None:
        return any(material.text is not None for material in context.task_contract.materials)
    if name == "task" and target_specs is not None:
        return any(expert_available(context, spec) for spec in target_specs.values())
    return True


class CapabilityDisclosureMiddleware(AgentMiddleware):
    """Only narrow registered capabilities; skills can never add tools or permissions."""

    def __init__(self, target_specs=None):
        self.target_specs = dict(target_specs or {})

    # 过滤模型可见工具，并将文档引用和专家目标限定为本轮可用值。
    def prepare(self, request):
        context = get_run_context()
        if context is None:
            return request
        call = active_call.get()
        category = "sub_model" if call and call.task_id else "main_model"
        owner = call.call_id if call else context.run_id
        # Current reasoning, a tool call and the final answer must all fit.
        can_use_tools = calls_available(
            category, "tool", category, *(("main_model",) if category == "sub_model" else ())
        )
        if call and call.task_id:
            can_use_tools &= tools_admitted(context)
        elif not context.report_started and coordination_window(context) <= 0:
            decision = admit_stage(context)
            close_limited_work(context, decision)
            context.start_report(decision.reason)
            can_use_tools = False
        evidence = tool_evidence.get() or []
        document_states = []
        if call and call.actor == "document" and context.task_contract is not None:
            from agent.runtime.document_work import document_progress

            row = context.task_results[call.task_id]
            item = context.required_work.get(row["work_item_id"])
            document_states = document_progress(context, item, task_id=call.task_id)
            from agent.runtime.workflows import _range_covered

            read, documents = set(), {}
            for record in evidence:
                if record.get("status") != "completed" or record.get("task_id") != call.task_id:
                    continue
                artifact = record.get("artifact") or {}
                if record.get("name") == "read_document":
                    coverage = artifact["coverage"]
                    if coverage["scope"] == "catalog":
                        continue
                    identity = tuple(artifact["document"][key] for key in ("path", "sha256", "parser"))
                    state = documents.setdefault(
                        identity, dict(extents=coverage["extents"], ranges={}, missing=set(), complete=False)
                    )
                    state["complete"] |= coverage["complete"]
                    state["missing"].update(coverage["missing"])
                    for target, bounds in coverage["read_ranges"].items():
                        state["ranges"].setdefault(target, []).append(bounds)
                if record.get("name") == "read_bound_material":
                    read.add(record["request_parameters"]["reference"])
            for (path, _, _), state in documents.items():
                covered = (
                    state["complete"]
                    or bool(state["extents"])
                    and all(
                        _range_covered(extent, state["ranges"].get(target, []))
                        for target, extent in state["extents"].items()
                    )
                )
                if covered and not state["missing"]:
                    read.add(path)
            exhausted = {state["reference"] for state in document_states if state["next_selection"] is None}
            materials = {material.reference for material in context.task_contract.materials}
            if materials and materials <= read | exhausted:
                can_use_tools = False
        page_sources = source_urls(evidence) or list(cited_urls(context.task_instruction))
        tools = []
        for tool in request.tools:
            if not can_use_tools:
                break
            if isinstance(tool, BaseTool):
                name, network = tool.name, adapter_for_tool(tool).network
            else:
                schema = convert_to_openai_tool(tool)
                name, network = schema["function"]["name"], False
            if not tool_available(context, name, network=network, target_specs=self.target_specs):
                continue
            if name in NETWORK_ACTIONS and not search_call_available(name, owner):
                continue
            if name == "read_web_page" and not page_sources:
                continue
            if name in ("read_document", "read_bound_material") and context.task_contract is not None:
                references = [
                    material.reference
                    for material in context.task_contract.materials
                    if (material.kind == "upload" if name == "read_document" else material.text is not None)
                ]
                if name == "read_document":
                    exhausted = {state["reference"] for state in document_states if state["next_selection"] is None}
                    references = [reference for reference in references if reference not in exhausted]
                if not references:
                    continue
                schema = deepcopy(convert_to_openai_tool(tool))
                parameter = "filename" if name == "read_document" else "reference"
                schema["function"]["parameters"]["properties"][parameter]["enum"] = references
                tool = (
                    tool.model_copy(update={"args_schema": schema["function"]["parameters"]})
                    if isinstance(tool, BaseTool)
                    else schema
                )
            if name == "task":
                targets = {
                    target: spec for target, spec in self.target_specs.items() if admit_stage(context, spec).allowed
                }
                for target, spec in self.target_specs.items():
                    record_decision(context, admit_stage(context, spec), "disclosure", target)
                if not targets:
                    continue
                schema = deepcopy(convert_to_openai_tool(tool))
                function = schema["function"]
                function["parameters"]["properties"]["subagent_type"] = {"type": "string", "enum": list(targets)}
                function["parameters"]["additionalProperties"] = False
                function["description"] = (
                    "仅委派当前允许的专家；description 包含目标、范围和限制，具体方法由专家选择。\n"
                    + "\n".join(
                        f"- {target}: {spec.display_name}；{spec.description}\n"
                        + "\n".join(
                            f"  {tool.name}: {tool.description}"
                            for tool in spec.tools
                            if tool_available(context, tool.name, network=adapter_for_tool(tool).network)
                        )
                        for target, spec in targets.items()
                    )
                )
                tool = (
                    tool.model_copy(
                        update={"description": function["description"], "args_schema": function["parameters"]}
                    )
                    if isinstance(tool, BaseTool)
                    else schema
                )
            tools.append(tool)
        settings = dict(request.model_settings)
        choice = request.tool_choice
        instruction = (
            "本回合只可使用当前提供的工具；额度耗尽的工具已移除，不得再次请求。"
            "没有可用取证工具或现有证据已足够时，直接根据实际结果返回结论、来源与缺口。"
            "不得通过重复委派或改用文件工具绕过额度；不为加载更多技能而延后已可交付的答复。"
        )
        if document_states:
            instruction += (
                "\n运行时文档进度（目录不是正文证据；接续仅选择未读范围，识别缺口如实交接，不重复失败操作）："
                + json.dumps(document_states, ensure_ascii=False)
            )
        if not (call and call.task_id):
            instruction += (
                "\n本轮原始请求与必做事项快照（仅为任务状态，不是事实证据；不得由摘要删减）："
                + json.dumps(
                    dict(
                        user_request=context.task_instruction,
                        network_policy=context.network_policy,
                        required_work=required_work_snapshot(context),
                    ),
                    ensure_ascii=False,
                )
                + "\n多阶段任务先登记全部必做事项，再逐项执行；最后再次对照原始请求检查遗漏。"
                "只有运行时已确认的 completed 才表示该事项完成；保留其他事项及原因。"
            )
        if not tools:
            instruction += "当前已进入最终答复回合，请输出自然语言结论正文，不输出函数调用；明确披露未完成事项。"
        if any(convert_to_openai_tool(tool)["function"]["name"] in NETWORK_ACTIONS for tool in request.tools):
            limits = context.policy.search
            with context.budget_lock:
                remaining = {
                    name: max(
                        0,
                        min(
                            maximum - context.search_counts.get((owner, name), 0),
                            limits.calls_per_run
                            - sum(count for (_, tool), count in context.search_counts.items() if tool == name),
                        ),
                    )
                    for name, maximum in (
                        ("internet_search", limits.calls_per_task),
                        ("read_web_page", limits.page_reads_per_task),
                    )
                }
            instruction += (
                f"当前分工剩余搜索 {remaining['internet_search']} 次、网页读取 {remaining['read_web_page']} 次。"
            )
            domains = sorted({urlsplit(url).hostname for url in page_sources if urlsplit(url).hostname})
            instruction += (
                "当前可读取的来源域名："
                + ("、".join(domains) or "无")
                + "。其他域名必须先通过本分工搜索发现；搜索额度耗尽时，使用已取得的来源答复并说明缺口。"
            )
        prepared = request.override(
            tools=tools,
            tool_choice=choice,
            model_settings=settings,
            system_message=append_system(request.system_message, instruction),
        )
        return prepare_final_request(prepared) if not tools else prepared

    @staticmethod
    def validate_response(request, response):
        result = getattr(response, "model_response", response)
        messages = getattr(result, "result", [result])
        if not request.tools:
            if any(
                getattr(message, "tool_calls", None)
                or getattr(message, "invalid_tool_calls", None)
                or getattr(message, "additional_kwargs", {}).get("function_call")
                for message in messages
            ):
                raise PolicyError(
                    "Model returned tool calls during a final-answer-only turn; no further calls are allowed"
                )
            if not any(isinstance(message, AIMessage) and message.text.strip() for message in messages):
                raise PolicyError("Final-answer model response must contain non-empty assistant text")
        return response

    def wrap_model_call(self, request, handler):
        prepared = self.prepare(request)
        with final_answer_mode(not prepared.tools):
            return self.validate_response(prepared, handler(prepared))

    async def awrap_model_call(self, request, handler):
        prepared = self.prepare(request)
        with final_answer_mode(not prepared.tools):
            return self.validate_response(prepared, await handler(prepared))

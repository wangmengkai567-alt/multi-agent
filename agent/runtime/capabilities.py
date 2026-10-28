"""Project the locked execution policy into model tools and actual admission."""

from copy import deepcopy

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from agent.runtime.context import get_run_context
from agent.runtime.evidence_protocol import adapter_for_tool

FILE_ACTIONS = frozenset(("write_file", "edit_file", "generate_markdown", "execute"))
NETWORK_ACTIONS = frozenset(("internet_search", "read_web_page"))
READONLY_FILESYSTEM_PROMPT = """文件工具只读查看本轮文件与 /skills/ 说明。
read_file 使用从 0 开始的 offset 和 limit 分页。grep 为字面量匹配。
只使用本轮实际提供的工具；最终正文由程序自动保存为 Markdown。
"""


def expert_available(context, target, spec):
    return target not in context.unavailable_agents and not (
        spec.execution.network_access and context.network_policy == "forbidden"
    )


def tool_available(context, name, *, network=False, target_specs=None):
    if name in FILE_ACTIONS:
        return False
    if context is None:
        return True
    if (network or name in NETWORK_ACTIONS) and context.network_policy == "forbidden":
        return False
    if name == "task" and target_specs is not None:
        return any(expert_available(context, target, spec) for target, spec in target_specs.items())
    return True


class CapabilityDisclosureMiddleware(AgentMiddleware):
    """Only narrow registered capabilities; skills can never add tools or permissions."""

    def __init__(self, target_specs=None):
        self.target_specs = dict(target_specs or {})

    def prepare(self, request):
        context = get_run_context()
        if context is None:
            return request
        tools = []
        for tool in request.tools:
            if isinstance(tool, BaseTool):
                name, network = tool.name, adapter_for_tool(tool).network
            else:
                schema = convert_to_openai_tool(tool)
                name, network = schema["function"]["name"], False
            if not tool_available(context, name, network=network, target_specs=self.target_specs):
                continue
            if name == "read_document" and context.task_contract is not None:
                filenames = [
                    material.reference for material in context.task_contract.materials if material.kind == "upload"
                ]
                if not filenames:
                    continue
                schema = deepcopy(convert_to_openai_tool(tool))
                schema["function"]["parameters"]["properties"]["filename"]["enum"] = filenames
                tool = (
                    tool.model_copy(update={"args_schema": schema["function"]["parameters"]})
                    if isinstance(tool, BaseTool)
                    else schema
                )
            if name == "task":
                targets = {
                    target: spec
                    for target, spec in self.target_specs.items()
                    if expert_available(context, target, spec)
                }
                schema = deepcopy(convert_to_openai_tool(tool))
                function = schema["function"]
                function["parameters"]["properties"]["subagent_type"] = {"type": "string", "enum": list(targets)}
                function["parameters"]["additionalProperties"] = False
                function["description"] = "仅委派当前允许的专家；description 包含完整分工和限制。\n" + "\n".join(
                    f"- {target}: {spec.display_name}；{spec.description}" for target, spec in targets.items()
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
        if not tools:
            choice = "none"
            settings["tool_choice"] = "none"
        return request.override(tools=tools, tool_choice=choice, model_settings=settings)

    def wrap_model_call(self, request, handler):
        return handler(self.prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self.prepare(request))

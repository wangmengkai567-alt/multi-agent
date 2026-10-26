"""Project the locked execution policy into model tools and actual admission."""

from copy import deepcopy

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from agent.runtime.context import get_run_context
from agent.runtime.evidence_protocol import adapter_for_tool

FILE_ACTIONS = frozenset(("write_file", "edit_file", "generate_markdown", "convert_md_to_pdf"))
RESEARCH_ACTIONS = frozenset(("task", "plan_research", "research_step", "submit_research_job"))
NETWORK_ACTIONS = frozenset(
    ("plan_research", "research_step", "internet_search", "read_web_page", "submit_research_job")
)
READONLY_FILESYSTEM_PROMPT = """框架文件工具仅供只读查看本轮文件和 /skills/ 说明。
read_file 使用从 0 开始的 offset 和 limit 分页；Skill 正文需要完整读取，参考资源按任务需要读取。
grep 是字面量子串匹配，不是正则表达式。只使用本轮实际提供的工具；文件由程序在终审后生成。
"""


def expert_available(context, target, spec):
    if target in context.unavailable_agents or not context.collecting:
        return False
    contract = context.task_contract
    if spec.execution.network_access and (
        context.network_policy == "forbidden" or contract is not None and not contract.network_allowed
    ):
        return False
    return contract is None or contract.delivery_kind == "grounded"


def tool_available(context, name, *, network=False, target_specs=None):
    if context is None:
        return True
    contract = context.task_contract
    if contract is not None and (contract.delivery_kind != "grounded" or name in FILE_ACTIONS):
        return False
    if name == "execute":
        return False
    if not context.collecting and name in RESEARCH_ACTIONS:
        return False
    if (network or name in NETWORK_ACTIONS) and (
        context.network_policy == "forbidden" or contract is not None and not contract.network_allowed
    ):
        return False
    if name == "task" and target_specs is not None:
        return any(expert_available(context, target, spec) for target, spec in target_specs.items())
    if name == "submit_research_job":
        from agent.runtime.contracts import background_eligible

        return bool(
            contract is not None
            and background_eligible(contract)
            and context.job_service is not None
            and not context.background_job
            and context.execution_mode != "foreground"
        )
    if name == "get_research_job":
        return context.job_service is not None
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
            if name == "task":
                targets = {
                    target: spec
                    for target, spec in self.target_specs.items()
                    if expert_available(context, target, spec)
                }
                schema = deepcopy(convert_to_openai_tool(tool))
                function = schema["function"]
                function["parameters"]["properties"]["subagent_type"] = {"type": "string", "enum": list(targets)}
                function["description"] = "仅委派当前允许的专家；description 包含完整分工和限制。\n" + "\n".join(
                    f"- {target}: {spec.display_name}；{spec.description}；输入协议={spec.execution.input_kind}，输出=已审核事实报告"
                    for target, spec in targets.items()
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

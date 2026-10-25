"""Validated expert definitions; registry construction never creates model clients."""

import re
from dataclasses import asdict, dataclass, field
from types import MappingProxyType

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
RESERVED_NAMES = {
    "main",
    "general-purpose",
    "task",
    "execute",
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "write_todos",
}


@dataclass(frozen=True)
class ModelConfig:
    profile: str = "SUB"
    review_profile: str = "REVIEW"
    max_retries: int = 0

    def __post_init__(self):
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", p) for p in (self.profile, self.review_profile)):
            raise ValueError("Model profiles must be explicit environment role names")
        if type(self.max_retries) is not int or self.max_retries != 0:
            raise ValueError("Model SDK retries must be zero")


@dataclass(frozen=True)
class ReviewConfig:
    enabled: bool = False
    max_refinements: int = 1
    model_check: bool = False
    required_tools: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    require_sources: bool = False
    grounded_report: bool = False

    def __post_init__(self):
        if type(self.max_refinements) is not int or self.max_refinements not in (0, 1):
            raise ValueError("At most one refinement is allowed")
        if any(
            type(v) is not bool for v in (self.enabled, self.model_check, self.require_sources, self.grounded_report)
        ):
            raise ValueError("Review flags must be booleans")
        if self.grounded_report and not (self.enabled and self.model_check):
            raise ValueError("Grounded reports require enabled semantic review")
        for values in (self.required_tools, self.required_fields):
            if not isinstance(values, tuple) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError("Review requirements must be immutable non-empty strings")


@dataclass(frozen=True)
class ExecutionConfig:
    input_kind: str = "task"
    output_kind: str = "fact_report"
    source_kind: str = "other"
    network_access: bool = False
    receive_internal_results: bool = False

    def __post_init__(self):
        if self.input_kind not in ("task", "planned_research") or self.output_kind != "fact_report":
            raise ValueError("Unsupported expert input/output protocol")
        if self.source_kind not in ("db", "search", "other"):
            raise ValueError("Unsupported workflow source category")
        if any(type(v) is not bool for v in (self.network_access, self.receive_internal_results)):
            raise ValueError("Execution permissions must be booleans")
        if self.input_kind == "planned_research" and not self.network_access:
            raise ValueError("Planned public research requires network access")

    @property
    def planned(self):
        return self.input_kind == "planned_research"


@dataclass(frozen=True)
class ResourceConfig:
    group: str = "queries"
    concurrency: int = 4
    queue_capacity: int = 64

    def __post_init__(self):
        if not re.fullmatch(ID_PATTERN, self.group):
            raise ValueError("Invalid expert resource group")
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 128:
            raise ValueError("Invalid expert group concurrency")
        if type(self.queue_capacity) is not int or not 0 <= self.queue_capacity <= 1000:
            raise ValueError("Invalid expert queue capacity")


@dataclass(frozen=True)
class SummaryConfig:
    trigger_tokens: int = 12000
    keep_messages: int = 6
    truncate_after_messages: int = 20
    keep_argument_messages: int = 6

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError("Summary thresholds must be positive integers")
        if self.keep_argument_messages > self.truncate_after_messages:
            raise ValueError("Argument retention cannot exceed its trigger")

    def kwargs(self):
        return dict(
            trigger=("tokens", self.trigger_tokens),
            keep=("messages", self.keep_messages),
            trim_tokens_to_summarize=None,
            truncate_args_settings={
                "trigger": ("messages", self.truncate_after_messages),
                "keep": ("messages", self.keep_argument_messages),
            },
        )


@dataclass(frozen=True)
class SubagentSpec:
    agent_id: str
    display_name: str
    description: str
    system_prompt: str
    tools: tuple
    model_config: ModelConfig = field(default_factory=ModelConfig)
    review_config: ReviewConfig | None = None
    allowed_skill_ids: tuple[str, ...] = ()
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    delegate_targets: tuple[str, ...] = ()

    def validate(self):
        for value, expected in (
            (self.model_config, ModelConfig),
            (self.execution, ExecutionConfig),
            (self.resources, ResourceConfig),
            (self.summary, SummaryConfig),
        ):
            if not isinstance(value, expected):
                raise ValueError("Expert capability configuration has an invalid type")
        if (
            not isinstance(self.agent_id, str)
            or not re.fullmatch(ID_PATTERN, self.agent_id)
            or self.agent_id in RESERVED_NAMES
        ):
            raise ValueError("Invalid or reserved expert ID")
        if any(
            not isinstance(v, str) or not v.strip() for v in (self.display_name, self.description, self.system_prompt)
        ):
            raise ValueError("Expert display name, description and prompt are required")
        if not all(isinstance(v, tuple) for v in (self.tools, self.allowed_skill_ids, self.delegate_targets)):
            raise ValueError("Registry collections must be immutable tuples")
        if (
            not isinstance(self.review_config, ReviewConfig)
            or not self.review_config.enabled
            or not self.review_config.model_check
        ):
            raise ValueError("Every expert requires an explicit enabled semantic review policy")
        if any(not isinstance(t, BaseTool) for t in self.tools):
            raise ValueError("Expert tools must have a registered BaseTool schema")
        if not self.review_config.grounded_report:
            raise ValueError("Expert fact_report output requires grounded report validation")
        names = [t.name for t in self.tools]
        reserved = RESERVED_NAMES
        if len(set(names)) != len(names) or set(names) & reserved:
            raise ValueError("Duplicate or reserved expert tool name")
        if not set(self.review_config.required_tools).issubset(names):
            raise ValueError("Review requires a tool not provided by this expert")
        if self.execution.planned and "research_step" not in names:
            raise ValueError("Planned research requires the controlled research_step tool")
        if not self.execution.network_access and set(names) & {
            "research_step",
            "internet_search",
            "read_web_page",
            "submit_research_job",
        }:
            raise ValueError("Network tool contradicts the expert permission")
        if self.execution.source_kind == "db" and self.execution.planned:
            raise ValueError("Database results cannot use the public research lifecycle")
        if len(set(self.delegate_targets)) != len(self.delegate_targets):
            raise ValueError("Duplicate delegation target")
        from agent.runtime.skills import validate_skill_ids

        validate_skill_ids(self.allowed_skill_ids)
        from agent.runtime.evidence_protocol import adapter_for_tool

        for t in self.tools:
            adapter = adapter_for_tool(t)
            if t.name in self.review_config.required_tools and adapter.kind == "control" and adapter.capture:
                raise ValueError("A required evidence tool cannot be declared as workflow control")
            if adapter.network and not self.execution.network_access:
                raise ValueError("Tool evidence binding contradicts the expert network permission")
            convert_to_openai_tool(t)

    def contract(self):
        from agent.runtime.evidence_protocol import adapter_for_tool

        return dict(
            agent_id=self.agent_id,
            display_name=self.display_name,
            description=self.description,
            system_prompt=self.system_prompt,
            tools=[
                dict(schema=convert_to_openai_tool(t), evidence_adapter=adapter_for_tool(t).adapter_id)
                for t in self.tools
            ],
            model_config=asdict(self.model_config),
            review_config=asdict(self.review_config),
            allowed_skill_ids=list(self.allowed_skill_ids),
            execution=asdict(self.execution),
            resources=asdict(self.resources),
            summary=asdict(self.summary),
            delegate_targets=list(self.delegate_targets),
        )


class SubagentRegistry:
    def __init__(self, specs):
        registered, groups = {}, {}
        for spec in specs:
            if not isinstance(spec, SubagentSpec):
                raise ValueError("Registry requires SubagentSpec definitions")
            spec.validate()
            if spec.agent_id in registered or any(s.display_name == spec.display_name for s in registered.values()):
                raise ValueError("Duplicate expert ID or display name")
            limits = (spec.resources.concurrency, spec.resources.queue_capacity)
            if spec.resources.group in groups and groups[spec.resources.group] != limits:
                raise ValueError("One resource group must have one set of limits")
            groups[spec.resources.group] = limits
            registered[spec.agent_id] = spec
        if not registered:
            raise ValueError("Expert registry cannot be empty")
        all_tools = {t.name for s in registered.values() for t in s.tools}
        if all_tools & registered.keys():
            raise ValueError("Expert and tool names must be distinct")
        visiting, visited = set(), set()

        def visit(key):
            if key in visiting:
                raise ValueError("Cyclic expert delegation is forbidden")
            if key in visited:
                return
            visiting.add(key)
            for target in registered[key].delegate_targets:
                if target not in registered:
                    raise ValueError("Unknown delegated expert")
                if not registered[key].execution.network_access and registered[target].execution.network_access:
                    raise ValueError("Delegation cannot escalate network permissions")
                visit(target)
            visiting.remove(key)
            visited.add(key)

        for key in registered:
            visit(key)
        self.specs = MappingProxyType(registered)

    def get(self, agent_id):
        if agent_id not in self.specs:
            raise ValueError(f"Unknown expert target: {agent_id}")
        return self.specs[agent_id]

    def contract(self):
        return {key: spec.contract() for key, spec in self.specs.items()}


def load_default_registry():
    from agent.subagents.definitions import default_specs

    return SubagentRegistry(default_specs())

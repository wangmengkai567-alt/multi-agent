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
    max_retries: int = 0

    def __post_init__(self):
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", p) for p in (self.profile,)):
            raise ValueError("Model profiles must be explicit environment role names")
        if type(self.max_retries) is not int or self.max_retries != 0:
            raise ValueError("Model SDK retries must be zero")


@dataclass(frozen=True)
class ExecutionConfig:
    source_kind: str = "other"
    network_access: bool = False

    def __post_init__(self):
        if self.source_kind not in ("db", "search", "other") or type(self.network_access) is not bool:
            raise ValueError("Invalid expert execution permissions")


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
    allowed_skill_ids: tuple[str, ...] = ()
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)

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
        if not all(isinstance(v, tuple) for v in (self.tools, self.allowed_skill_ids)):
            raise ValueError("Registry collections must be immutable tuples")
        if any(not isinstance(t, BaseTool) for t in self.tools):
            raise ValueError("Expert tools must have a registered BaseTool schema")
        names = [t.name for t in self.tools]
        reserved = RESERVED_NAMES
        if len(set(names)) != len(names) or set(names) & reserved:
            raise ValueError("Duplicate or reserved expert tool name")
        from agent.runtime.skills import validate_skill_ids

        validate_skill_ids(self.allowed_skill_ids)
        from agent.runtime.evidence_protocol import adapter_for_tool

        for t in self.tools:
            adapter = adapter_for_tool(t)
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
            allowed_skill_ids=list(self.allowed_skill_ids),
            execution=asdict(self.execution),
            resources=asdict(self.resources),
            summary=asdict(self.summary),
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

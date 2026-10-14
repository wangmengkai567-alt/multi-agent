"""Explicit, immutable expert registry; importing it creates no clients."""

from dataclasses import dataclass, field
from types import MappingProxyType

DISPLAY_NAMES = {"db": "数据分析助手", "search": "市场情报助手", "knowledge": "内部知识助手"}


@dataclass(frozen=True)
class ModelConfig:
    profile: str = "default"
    max_retries: int = 0


@dataclass(frozen=True)
class ReviewConfig:
    enabled: bool = False
    max_refinements: int = 1


@dataclass(frozen=True)
class SubagentSpec:
    agent_id: str
    display_name: str
    description: str
    system_prompt: str
    tools: tuple
    model_config: ModelConfig = field(default_factory=ModelConfig)
    review_config: ReviewConfig = field(default_factory=ReviewConfig)
    allowed_skill_ids: tuple[str, ...] = ()


class SubagentRegistry:
    def __init__(self, specs):
        registered = {}
        for spec in specs:
            if spec.agent_id not in DISPLAY_NAMES or spec.agent_id in registered:
                raise ValueError("Invalid or duplicate expert ID")
            if not spec.display_name or not spec.description or not spec.system_prompt:
                raise ValueError("Expert display name, description and prompt are required")
            if not isinstance(spec.tools, tuple) or not isinstance(spec.allowed_skill_ids, tuple):
                raise ValueError("Registry collections must be immutable tuples")
            if spec.model_config != ModelConfig() or spec.review_config != ReviewConfig() or spec.allowed_skill_ids:
                raise ValueError("Custom model profiles, review and Skill loading are not yet enabled")
            registered[spec.agent_id] = spec
        self.specs = MappingProxyType(registered)

    def get(self, agent_id):
        if agent_id not in self.specs:
            raise ValueError(f"Unknown expert target: {agent_id}")
        return self.specs[agent_id]


def load_default_registry():
    # Legacy definitions supply prompts/tools only. Their Chinese names are
    # display labels; factory and framework routing always use stable IDs.
    from agent.subagents.database_query_agent import database_query_agent
    from agent.subagents.knowledge_base_agent import knowledge_base_agent
    from agent.subagents.network_search_agent import network_search_agent

    sources = (("db", database_query_agent), ("search", network_search_agent), ("knowledge", knowledge_base_agent))
    return SubagentRegistry(
        [
            SubagentSpec(
                agent_id,
                DISPLAY_NAMES[agent_id],
                source["description"],
                source["system_prompt"],
                tuple(source["tools"]),
            )
            for agent_id, source in sources
        ]
    )

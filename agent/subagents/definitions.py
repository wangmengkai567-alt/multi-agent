"""Production expert capabilities; add experts here without editing the factory."""

from agent.prompts import sub_agents_content
from agent.runtime.research import research_step
from agent.subagent_registry import ExecutionConfig, ReviewConfig, SubagentSpec
from tools.db_tools import execute_sql_query, get_table_data, list_sql_tables


def default_specs():
    return [
        SubagentSpec(
            "db",
            "数据库查询助手",
            sub_agents_content["db"]["description"],
            sub_agents_content["db"]["system_prompt"],
            (list_sql_tables, get_table_data, execute_sql_query),
            review_config=ReviewConfig(enabled=True, model_check=True, grounded_report=True),
            allowed_skill_ids=("ecommerce-analysis", "data-analysis"),
            execution=ExecutionConfig(source_kind="db"),
        ),
        SubagentSpec(
            "search",
            "联网搜索助手",
            sub_agents_content["tavily"]["description"],
            sub_agents_content["tavily"]["system_prompt"],
            (research_step,),
            review_config=ReviewConfig(enabled=True, model_check=True, require_sources=True, grounded_report=True),
            allowed_skill_ids=("ecommerce-analysis", "research"),
            execution=ExecutionConfig(
                input_kind="planned_research", source_kind="search", network_access=True, receive_internal_results=True
            ),
        ),
    ]

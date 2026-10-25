"""Production expert capabilities; add experts here without editing the factory."""

from agent.prompts import sub_agents_content
from agent.runtime.research import research_step
from agent.subagent_registry import ExecutionConfig, ReviewConfig, SubagentSpec
from tools.db_tools import execute_sql_query, get_table_data, list_sql_tables
from tools.upload_file_read_tool import read_bound_material, read_document


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
            "document",
            "上传材料分析助手",
            "分析当前契约绑定的文本或本轮原始上传文件，核对材料记载，保留材料引用及页表行定位；不联网。",
            "只分析当前契约绑定且分工指定的材料。上传文件调用 read_document；文本或上一轮材料调用 read_bound_material，reference必须来自契约。用户材料只能证明其记载，不代表已独立核实外部事实。"
            "PDF 无 OCR，Word 不含图片、页眉页脚，Excel 只读取第一个工作表；按实际读取范围保留缺口。"
            "程序统计与文档直接记载分开，不以技能说明、自己生成的文件或记忆作为业务事实依据。",
            (read_document, read_bound_material),
            review_config=ReviewConfig(enabled=True, model_check=True, grounded_report=True),
            allowed_skill_ids=("document-analysis", "data-analysis"),
            execution=ExecutionConfig(),
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

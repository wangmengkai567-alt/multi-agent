"""The small production expert set and its explicit permissions."""

from agent.subagent_registry import ExecutionConfig, SubagentSpec
from tools.db_tools import execute_sql_query, get_table_data, list_sql_tables
from tools.tavily_tool import internet_search, read_web_page
from tools.upload_file_read_tool import read_bound_material, read_document


def default_specs():
    return [
        SubagentSpec(
            "db",
            "数据库查询助手",
            "查询获准业务表，核对实际 SQL、时间和统计口径。",
            "只使用允许的只读数据库工具。先用 list_sql_tables 确认表，再用 get_table_data 取得真实表结构和有界样本。"
            "只用实际返回的列完成分派的 SELECT 查询，保留实际 SQL、结果和局限。"
            "get_table_data 已返回字段类型、可空性和键；业务定义未给出时明确未知，不追加猜测列或跨库元数据查询。"
            "取得本次所需结果后直接返回，不为未要求的业务维度继续探测。",
            (list_sql_tables, get_table_data, execute_sql_query),
            allowed_skill_ids=("ecommerce-analysis", "data-analysis"),
            execution=ExecutionConfig(source_kind="db"),
        ),
        SubagentSpec(
            "document",
            "上传材料分析助手",
            "读取本轮上传文件或绑定文本，保留页、表、行位置和读取限制。",
            "只分析本轮绑定材料。上传文件调用 read_document，文本调用 read_bound_material。"
            "read_document 已返回原文、位置和读取范围，取得所需内容后直接回答分工。"
            "read_bound_material 仅接受本轮披露的文本 reference，上传文件路径不能作为该工具的 reference。"
            "依据实际解析结果说明位置和读取限制；材料记载不等于独立核实的外部事实。",
            (read_document, read_bound_material),
            allowed_skill_ids=("document-analysis", "data-analysis"),
        ),
        SubagentSpec(
            "search",
            "联网搜索助手",
            "按分工搜索公开资料，必要时读取网页，返回实际来源 URL。",
            "按分工直接调用 internet_search，必要时 read_web_page。优先使用官方一手来源，保留日期、来源和缺口。"
            "源码任务应定位并读取相关源码文件；搜索摘要与已读取正文分开说明，不猜测没有取得的数据或来源。",
            (internet_search, read_web_page),
            allowed_skill_ids=("ecommerce-analysis", "research"),
            execution=ExecutionConfig(source_kind="search", network_access=True),
        ),
    ]

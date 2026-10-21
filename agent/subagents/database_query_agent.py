from agent.prompts import sub_agents_content
from tools.db_tools import execute_sql_query, get_table_data, list_sql_tables

database_query_agent = {
    "name": sub_agents_content["db"]["name"],
    "description": sub_agents_content["db"]["description"],
    "system_prompt": sub_agents_content["db"]["system_prompt"],
    "tools": [list_sql_tables, get_table_data, execute_sql_query],
}

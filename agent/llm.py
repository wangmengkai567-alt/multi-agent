import json
import os

from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

from agent.runtime.policies import load_runtime_policy


def model_options(raw):
    if not raw or not raw.strip():
        return {}
    try:
        extra = json.loads(raw)
    except ValueError as exc:
        raise ValueError("LLM_EXTRA_BODY must be a JSON object") from exc
    if not isinstance(extra, dict):
        raise ValueError("LLM_EXTRA_BODY must be a JSON object")
    return {"extra_body": extra}


# 加载配置文件
# find_dotenv() 确保找到 .env文件 递归查询当前项目文件夹
load_dotenv(find_dotenv())

model = init_chat_model(
    model=os.getenv("LLM_QWEN_MAX"),
    model_provider="openai",
    max_retries=0,
    **model_options(os.getenv("LLM_EXTRA_BODY")),
    timeout=load_runtime_policy().timeouts.request_seconds,
)

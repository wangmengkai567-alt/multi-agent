"""Lazy RAGFlow queries with bounded HTTP requests and explicit errors."""

import requests
from langchain_core.tools import tool
from ragflow_sdk import RAGFlow

from agent.runtime.policies import MissingConfiguration, policy_for_run
from api.monitor import monitor
from rawflow.rag_config import _load_ragflow_env


class BoundedRAGFlow(RAGFlow):
    def _request(self, method, path, **kwargs):
        response = requests.request(
            method,
            self.api_url + path,
            headers=self.authorization_header,
            timeout=policy_for_run().sql.timeout_seconds,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def get(self, path, params=None, json=None):
        return self._request("GET", path, params=params, json=json)

    def post(self, path, json=None, stream=False, files=None):
        return self._request("POST", path, json=json, stream=stream, files=files)

    def delete(self, path, json):
        return self._request("DELETE", path, json=json)

    def put(self, path, json):
        return self._request("PUT", path, json=json)


def get_client():
    api_key, base_url = _load_ragflow_env()
    if not api_key or not base_url:
        raise MissingConfiguration("RAGFlow configuration is missing")
    return BoundedRAGFlow(api_key=api_key, base_url=base_url)


@tool
def get_assistant_list() -> str:
    """List available RAGFlow assistants and their datasets."""
    monitor.report_tool("ragflow聊天助手列表查询工具：get_assistant_list")
    chats = get_client().list_chats()
    if not chats:
        return "没有任何可用助手"
    return "\n".join(
        f"助手名称:{chat.name};功能介绍：{chat.description}; 关联的知识库："
        + "、".join(dataset["name"] for dataset in chat.datasets or [])
        for chat in chats
    )


@tool
def create_ask_delete(chat_name: str, question: str) -> str:
    """Query an existing assistant in a temporary session and always clean it up."""
    if not chat_name.strip() or not question.strip():
        raise ValueError("Assistant name and question must not be empty")
    monitor.report_tool("ragflow提问助手工具：create_ask_delete", {"chat_name": chat_name, "question": question})
    chats = get_client().list_chats(name=chat_name)
    if not chats:
        raise ValueError("Requested assistant does not exist")
    chat = chats[0]
    session = chat.create_session(name="temp_session_ask")
    try:
        result = ""
        for part in session.ask(question=question, stream=True):
            result = part.content
        return result
    finally:
        chat.delete_sessions(ids=[session.id])

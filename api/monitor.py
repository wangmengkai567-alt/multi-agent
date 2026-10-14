import asyncio
import datetime
import logging
from typing import Any

from fastapi import WebSocket

from agent.runtime.context import get_run_context
from agent.runtime.events import make_event, redact_text, sanitize
from api.context import get_thread_context

# 尝试导入全局运行时（用于脚本模式下的流式输出）
try:
    import builtins
except ImportError:
    builtins = None


class ToolMonitor:
    """
    工具监控类，用于在工具执行过程中上报进度和状态。
    设计为单例模式，可在任何工具中直接导入使用。
    兼容 FastAPI WebSocket 和 脚本运行时的 stream_writer。

    使用示例:
    from api.monitor import monitor

    def my_tool(arg1):
        monitor.report_start("my_tool", {"arg1": arg1})
        ...
        monitor.report_running("my_tool", "正在处理数据...", progress=0.5)
        ...
        monitor.report_end("my_tool", result)
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.websocket_manager = None  # 预留给 FastAPI WebSocketManager
        return cls._instance

    def set_websocket_manager(self, manager):
        """设置 FastAPI 的 WebSocket 管理器"""
        self.websocket_manager = manager

    def _emit(self, event_type, message, data=None, *, actor="main", status="info", call_id=None, parent_call_id=None):
        context = get_run_context()
        if context is not None and context.event_sink is not None:
            event = make_event(
                context.thread_id,
                context.run_id,
                event_type,
                message,
                data,
                actor=actor,
                status=status,
                call_id=call_id,
                parent_call_id=parent_call_id or context.run_id,
            )
            context.event_sink.submit(event)
            return
        # Explicit standalone compatibility; no claim of persistence.
        payload = {
            "type": "monitor_event",
            "event": event_type,
            "message": redact_text(message),
            "data": sanitize(data or {}),
            "timestamp": datetime.datetime.now().isoformat(),
        }
        if self.websocket_manager and get_thread_context():
            try:
                self.websocket_manager.schedule_delivery(payload, get_thread_context())
            except Exception:
                logging.getLogger(__name__).exception("Standalone monitor delivery failed")
        if builtins and hasattr(builtins, "runtime") and hasattr(builtins.runtime, "stream_writer"):
            try:
                builtins.runtime.stream_writer(payload)
            except Exception:
                logging.getLogger(__name__).exception("Standalone runtime delivery failed")
        print(f"[Monitor:{event_type}] {payload['message']}")

    def report_tool(self, tool_name: str, args: dict[str, Any] = None):
        self._emit(
            "tool_start",
            f"开始执行工具: {tool_name}",
            {"tool_name": tool_name, "args": args},
            actor=tool_name,
            status="started",
        )

    def report_assistant(self, assistant_name: str, args: dict[str, Any] = None, *, call_id=None):
        self._emit(
            "assistant_call",
            f"模型请求助手: {assistant_name}",
            {"assistant_name": assistant_name, "args": args},
            actor=assistant_name,
            status="requested",
            call_id=call_id,
        )

    def report_tool_request(self, name, args, call_id):
        self._emit(
            "tool_requested",
            f"模型请求工具: {name}",
            {"tool_name": name, "args": args},
            actor=name,
            status="requested",
            call_id=call_id,
        )

    def report_tool_result(self, message):
        self._emit(
            "tool_returned",
            "工具调用返回",
            {"tool_name": message.name, "result_length": len(str(message.content))},
            actor=message.name or "tool",
            status="failed" if message.status == "error" else "returned",
            call_id=message.tool_call_id,
        )

    def report_task_result(self, result: str):
        self._emit("task_result", "任务执行完成", {"result": result}, status="completed")

    def report_session_dir(self, path: str):
        """报告任务工作目录"""
        self._emit("session_created", "本轮工作目录已创建", {"path": path}, actor="workspace", status="ready")


# 全局单例实例
monitor = ToolMonitor()


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}
        # 延迟绑定 loop，防止初始化时 loop 不一致
        self.loop = None
        self.delivery_tasks = set()
        self._closing = False

    def set_loop(self, loop):
        """显式设置事件循环"""
        self.loop = loop
        self._closing = False
        monitor.set_websocket_manager(self)
        print(f"[Monitor] ConnectionManager manually bound to loop: {id(self.loop)}")

    def schedule_delivery(self, payload, thread_id):
        """Called from either an async agent or a synchronous tool thread."""
        loop = self.loop
        if loop is None or loop.is_closed() or self._closing:
            return

        def start():
            if self._closing or self.loop is not loop:
                return
            self.track_delivery(loop.create_task(self.send_to_thread(payload, thread_id)))

        loop.call_soon_threadsafe(start)

    def track_delivery(self, task):
        # Only actual asyncio Tasks on the service loop are registered. Cancelling
        # a run_coroutine_threadsafe Future alone would not await its coroutine.
        self.delivery_tasks.add(task)
        task.add_done_callback(self._delivery_finished)

    def _delivery_finished(self, task):
        self.delivery_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logging.getLogger(__name__).error(
                    "Monitor delivery failed", exc_info=(type(error), error, error.__traceback__)
                )

    async def shutdown(self):
        self._closing = True
        # Flush scheduled callbacks after agent executions have stopped.
        await asyncio.sleep(0)
        tasks = list(self.delivery_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        self.delivery_tasks.clear()
        for websocket in list(self.active_connections.values()):
            try:
                await websocket.close()
            except Exception:
                logging.getLogger(__name__).exception("WebSocket close failed")
        self.active_connections.clear()
        self.loop = None
        monitor.set_websocket_manager(None)

    async def connect(self, websocket: WebSocket, thread_id: str):
        await websocket.accept()
        print(f"存储当前会话id:{thread_id}对应的:{websocket}")
        self.active_connections[thread_id] = websocket
        print(f"Client connected: {thread_id}")

    def disconnect(self, websocket: WebSocket, thread_id: str):
        if thread_id in self.active_connections:
            del self.active_connections[thread_id]
        print(f"Client disconnected: {thread_id}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def send_to_thread(self, message: dict, thread_id: str):
        if thread_id in self.active_connections:
            websocket = self.active_connections[thread_id]
            await websocket.send_json(message)


manager = ConnectionManager()

# 受控子进程调用：阻塞的外部服务和文档解析通过 JSON 管道执行，超时或取消时回收进程。
"""Bounded local RPC: only fixed provider operations, no pickled handlers or run-context copies."""

import json
import queue
import sys
from pathlib import Path
from threading import BoundedSemaphore, Thread
from time import monotonic

from agent.runtime.context import get_run_context
from agent.runtime.errors import DependencyUnavailable, ExecutionTerminated
from agent.runtime.processes import OwnedProcess
from agent.runtime.recovery import ToolCallTimeout
from agent.runtime.tool_timeout import active_tool_scope

_slots = BoundedSemaphore(8)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class WorkerOperationError(RuntimeError):
    def __init__(self, data):
        self.category = data["category"]
        self.timed_out = data.get("timed_out", False)
        self.status_code, self.headers = data.get("status_code"), data.get("headers", {})
        super().__init__(data["message"])


def run_operation(operation, payload, *, seconds, on_event=None):
    context, scope = get_run_context(), active_tool_scope.get()
    deadline = monotonic() + seconds
    if scope is not None and scope.deadline is not None:
        deadline = min(deadline, scope.deadline)

    def check():
        if context is not None:
            context.check_active()
        if scope is not None:
            scope.check()
        if monotonic() >= deadline:
            raise ToolCallTimeout(operation, seconds)

    # 等待进程容量时持续检查停止信号；排队时间同样计入本次操作时限。
    while not _slots.acquire(timeout=0.02):
        check()
    owned, reader, writer = None, None, None
    try:
        check()
        owned = OwnedProcess(
            [sys.executable, "-B", "-m", "agent.runtime.worker"], cwd=Path(__file__).resolve().parents[2]
        )
        if scope is not None:
            scope.attach(owned)
        process, received = owned.process, queue.Queue()

        def read():
            try:
                while line := process.stdout.readline(MAX_RESPONSE_BYTES + 1):
                    if len(line) > MAX_RESPONSE_BYTES:
                        raise ValueError("Worker response exceeds the configured bound")
                    received.put(json.loads(line))
            except BaseException:
                received.put({"event": "protocol_error"})
            finally:
                received.put({"event": "closed"})

        def write():
            try:
                process.stdin.write(
                    (json.dumps(dict(operation=operation, payload=payload), ensure_ascii=False) + "\n").encode()
                )
                process.stdin.flush()
            except (OSError, ValueError):
                received.put({"event": "protocol_error"})

        reader = Thread(target=read, name="worker-output", daemon=True)
        writer = Thread(target=write, name="worker-input", daemon=True)
        reader.start()
        writer.start()
        while True:
            check()
            try:
                item = received.get(timeout=0.02)
            except queue.Empty:
                continue
            event = item.get("event")
            # 外部请求发出前先由主进程授权，授权成功才通知子进程继续发送。
            if event == "progress":
                if on_event is not None:
                    on_event(item)
                else:
                    raise ValueError("Unexpected worker progress event")
                check()
                process.stdin.write(b'{"continue":true}\n')
                process.stdin.flush()
            elif event == "result":
                check()
                return item["value"]
            elif event == "error":
                error = item["error"]
                if error["category"] == "dependency_unavailable":
                    raise DependencyUnavailable(error["message"])
                if error["kind"] in ("ValueError", "UnicodeDecodeError"):
                    raise ValueError(error["message"])
                if error["kind"] == "FileNotFoundError":
                    raise FileNotFoundError(error["message"])
                raise WorkerOperationError(error)
            else:
                raise WorkerOperationError(dict(category="execution", message="Worker exited without a valid result"))
    finally:
        # 无论成功失败都先确认子进程结束，再关闭管道并归还容量；回收失败不能继续放行。
        if owned is not None:
            owned.close()  # A failed reap retains its capacity slot and stops the run.
        if scope is not None and owned is not None:
            scope.detach(owned)
        for thread in (writer, reader):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=1)
                if thread.is_alive():
                    raise ExecutionTerminated("Contained worker pipe did not close after process termination")
        if owned is not None:
            for stream in (owned.process.stdin, owned.process.stdout):
                stream.close()
        _slots.release()

"""Bound synchronous waits without pretending a timed-out operation was rolled back."""

from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextvars import copy_context
from threading import BoundedSemaphore, Thread

from agent.runtime.recovery import ToolCallTimeout

_slots = BoundedSemaphore(8)


def invoke_bounded(handler, request, seconds):
    if not _slots.acquire(timeout=seconds):
        raise ToolCallTimeout(request.tool_call["name"], seconds)
    future, context = Future(), copy_context()

    def execute():
        try:
            future.set_result(context.run(handler, request))
        except BaseException as error:
            future.set_exception(error)
        finally:
            _slots.release()

    Thread(target=execute, daemon=True, name="bounded-tool").start()
    try:
        return future.result(timeout=seconds)
    except FutureTimeout as error:
        if future.done():
            raise
        raise ToolCallTimeout(request.tool_call["name"], seconds) from error

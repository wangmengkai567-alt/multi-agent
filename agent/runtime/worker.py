"""Trusted fixed-operation worker. The parent assigns containment before this module executes."""

import base64
import json
import sys
from pathlib import Path


def emit(value):
    sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
    sys.stdout.buffer.flush()


def progress(kind, **data):
    emit(dict(event="progress", kind=kind, **data))
    if json.loads(sys.stdin.buffer.readline()) != {"continue": True}:
        raise RuntimeError("Worker operation was not authorized to continue")


def dispatch(operation, payload):
    if operation == "dependency_probe":
        import importlib

        for module in payload["modules"]:
            importlib.import_module(module)
        return {"available": True}
    if operation == "document_read":
        from tools.upload_file_read_tool import _read_local

        raw, result = _read_local(Path(payload["path"]), payload["maximum"], payload["selection"])
        return dict(raw=base64.b64encode(raw).decode("ascii"), result=result)
    if operation == "database":
        from tools.db_tools import execute_worker

        return execute_worker(payload)
    if operation == "tavily":
        from tools.tavily_tool import execute_worker

        return execute_worker(payload, progress)
    raise ValueError("Unknown worker operation")


def main():
    request = json.loads(sys.stdin.buffer.readline())
    try:
        result = dispatch(request["operation"], request["payload"])
        emit(dict(event="result", value=result))
    except Exception as error:
        from agent.runtime.errors import DependencyUnavailable
        from agent.runtime.events import redact_text
        from agent.runtime.policies import classify_tool_error
        from agent.runtime.recovery import http_status, is_transport_timeout

        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None) or {}
        emit(
            dict(
                event="error",
                error=dict(
                    kind=type(error).__name__,
                    category="dependency_unavailable"
                    if isinstance(error, ImportError | DependencyUnavailable)
                    else classify_tool_error(error),
                    status_code=http_status(error),
                    timed_out=is_transport_timeout(error),
                    headers={key: str(value) for key, value in headers.items() if key.lower() == "retry-after"},
                    message=redact_text(str(error)),
                ),
            )
        )


if __name__ == "__main__":
    main()

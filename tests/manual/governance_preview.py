"""Local-only browser fixture using the production page, script and styles."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
seq = 0


def event(kind, call, parent, actor, status, data=None):
    global seq
    seq += 1
    return dict(
        thread_id="preview-thread",
        run_id="preview-run",
        event_seq=seq,
        event=kind,
        call_id=call,
        parent_call_id=parent,
        actor=actor,
        status=status,
        data=data or {},
        timestamp="2026-10-19T09:00:00Z",
    )


EVENTS = [event("run_started", "preview-run", None, "main", "running")]
for suffix, label in (("a", "晋城宜居性调查"), ("b", "新乡宜居性调查")):
    EVENTS += [
        event("assistant_call", "request-" + suffix, "preview-run", "search", "requested"),
        event(
            "subagent_running",
            "agent-" + suffix,
            "request-" + suffix,
            "search",
            "running",
            dict(target="search", request_call_id="request-" + suffix, task_label=label),
        ),
    ]
    for number, name in ((1, "internet_search"), (2, "read_web_page")):
        call = f"tool-{suffix}-{number}"
        EVENTS += [
            event("before_tool", call, "agent-" + suffix, "search:" + name, "running"),
            event(
                "after_tool",
                call,
                "agent-" + suffix,
                "search:" + name,
                "completed",
                dict(search_budget=dict(task_used=number, task_limit=6, run_used=number, run_limit=24)),
            ),
        ]
    EVENTS += [
        event(
            "subagent_completed",
            "agent-" + suffix,
            "request-" + suffix,
            "search",
            "completed",
            dict(target="search", request_call_id="request-" + suffix, check_status="passed"),
        )
    ]
EVENTS += [
    event(
        "task_result",
        "preview-run",
        None,
        "main",
        "completed",
        dict(result="浏览器验收回放：两位同类型助手、各自工具、额度和检查结果均按实例分组。此页使用隔离测试数据。"),
    )
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            response = {"files": [], "jobs": []}
            if self.path.split("?")[0].endswith("/history"):
                response = {
                    "run": {
                        "run_id": "preview-run",
                        "thread_id": "preview-thread",
                        "status": "completed",
                        "answer": EVENTS[-1]["data"]["result"],
                        "error": None,
                    },
                    "events": EVENTS,
                    "subtasks": [],
                    "results": [],
                }
            data = json.dumps(response, ensure_ascii=False).encode()
            kind = "application/json"
        elif self.path == "/fixture.js":
            data = (
                "const preview=createSession('preview-thread','城市比较 · 界面验收回放','preview-run');for(const event of "
                + json.dumps(EVENTS, ensure_ascii=False)
                + ")applyEvent(preview,event);"
            ).encode()
            kind = "text/javascript"
        else:
            path = ROOT / "api" / "static" / ("index.html" if self.path == "/" else self.path.removeprefix("/static/"))
            if not path.is_file() or not path.resolve().is_relative_to((ROOT / "api" / "static").resolve()):
                self.send_error(404)
                return
            data = path.read_bytes()
            kind = {".html": "text/html", ".js": "text/javascript"}.get(path.suffix, "text/plain")
            if path.suffix == ".html":
                data = data.replace(b"</body>", b'<script src="/fixture.js"></script></body>')
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print("PREVIEW http://127.0.0.1:" + str(server.server_port), flush=True)
    server.serve_forever()

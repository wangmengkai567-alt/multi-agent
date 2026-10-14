import asyncio
import logging
import shutil
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles as FastAPIStaticFiles
from pydantic import BaseModel, Field

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

# Import agent runner and monitor
from agent.main_agent import initialize_agent, run_deep_agent, shutdown_agent  # noqa: E402
from agent.runtime.context import RunContext, reset_run_context, set_run_context  # noqa: E402
from agent.runtime.events import EventSink  # noqa: E402
from agent.runtime.hooks import HookCall, finish_run, pipeline  # noqa: E402
from api.monitor import manager  # noqa: E402
from api.run_store import RunConflict, RunStore  # noqa: E402

logger = logging.getLogger(__name__)
run_db_path = project_root / "data" / "runs.db"


def _task_finished(task, run_id):
    app.state.agent_tasks.discard(task)
    if app.state.run_tasks.get(run_id) is task:
        del app.state.run_tasks[run_id]
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.error(
                "Agent execution/recording raised for run %s",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )


async def _execute_run(record, context):
    token = set_run_context(context)
    outcome = HookCall("main", context.run_id)
    answer = None
    error = None
    try:
        if not await context.event_sink.start(context):
            return
        pipeline.emit("before_run", outcome)
        answer = await run_deep_agent(record["question"], context.thread_id)
        outcome.status, outcome.result = "completed", answer
    except asyncio.CancelledError:
        outcome.status = "cancelled"
        error = "Service execution cancelled"
        pipeline.emit("on_error", HookCall("main", context.run_id, status="cancelled", error=asyncio.CancelledError()))
        raise
    except Exception as exc:
        outcome.status, outcome.error = "failed", exc
        error = str(exc)
        pipeline.emit("on_error", outcome)
        raise
    finally:
        try:
            if outcome.status != "running":
                await finish_run(context, outcome, answer=answer, error=error)
        finally:
            reset_run_context(token)


@asynccontextmanager
async def lifespan(app):
    app.state.agent_tasks = set()
    app.state.run_tasks = {}
    app.state.submission_lock = asyncio.Lock()
    app.state.stopping = False
    store = None
    sink = None
    try:
        await initialize_agent()
        store = await RunStore.open(run_db_path)
        app.state.run_store = store
        sink = EventSink(store, manager.schedule_delivery)
        app.state.event_sink = sink
        output_dir.mkdir(exist_ok=True)
        updated_dir.mkdir(exist_ok=True)
        manager.set_loop(asyncio.get_running_loop())
        yield
    finally:
        app.state.stopping = True
        # Wait for an admission already in progress before snapshotting tasks.
        async with app.state.submission_lock:
            tasks = list(app.state.agent_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if sink is not None:
                try:
                    await sink.interrupt_active("Service shut down before execution completed")
                finally:
                    await sink.close()
        finally:
            try:
                await manager.shutdown()
            finally:
                try:
                    if store is not None:
                        await store.close()
                finally:
                    await shutdown_agent()


app = FastAPI(title="DeepAgents API", lifespan=lifespan)

# 挂载输出目录，以便前端访问生成的静态文件
# 假设输出目录位于项目根目录下的 output
output_dir = project_root / "output"


# 定义上传目录 updated
updated_dir = project_root / "updated"


# 挂载静态文件目录 (前端调试页面)
static_dir = current_dir / "static"

app.mount("/static", FastAPIStaticFiles(directory=str(static_dir)), name="static")

# 挂载输出目录 (前端可直接访问生成的文件)
app.mount("/output", FastAPIStaticFiles(directory=str(output_dir), check_dir=False), name="output")


# 首页
@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    return "<h1>MultiAgent-Search API</h1><p>访问 <a href='/docs'>/docs</a> 查看 API 文档</p>"


# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    query: str = Field(min_length=1)
    thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")


@app.post("/api/task")
async def run_task(request: TaskRequest):
    thread_id = request.thread_id or str(uuid.uuid4())
    async with app.state.submission_lock:
        if app.state.stopping:
            raise HTTPException(status_code=503, detail="Service is shutting down")
        run_id = str(uuid.uuid4())
        try:
            record = await app.state.event_sink.create(thread_id, request.query, run_id)
        except asyncio.CancelledError:
            # A cancelled await can race a committed insert in the DB worker.
            # The server-generated ID lets us settle precisely that reservation.
            await app.state.event_sink.finish(
                RunContext(run_id, thread_id, output_dir), "cancelled", error="Task submission cancelled"
            )
            raise
        except RunConflict as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "busy",
                    "thread_id": thread_id,
                    "run_id": exc.run_id,
                },
            )
        run_id = record["run_id"]
        context = RunContext(
            run_id, thread_id, output_dir / f"session_{thread_id}" / f"run_{run_id}", event_sink=app.state.event_sink
        )
        record = dict(record, question=request.query)
        task = asyncio.create_task(_execute_run(record, context), name=f"run:{run_id}")
        app.state.agent_tasks.add(task)
        app.state.run_tasks[run_id] = task
        task.add_done_callback(lambda completed: _task_finished(completed, run_id))
    return {"status": "started", "thread_id": thread_id, "run_id": run_id}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    record = await app.state.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return record


@app.get("/api/runs/{run_id}/history")
async def get_run_history(run_id: str):
    history = await app.state.run_store.history(run_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return history


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...), thread_id: str = Form(...)):
    """
    文件上传接口 (File Upload)。

    目标：
    1. 接收用户上传的一个或多个文件。
    2. 保存到 `updated/session_{thread_id}` 目录。
    3. 供 Agent 在后续任务中读取和分析。

    Args:
        files (list[UploadFile]): 文件对象列表。
        thread_id (str): 关联的任务会话 ID。
    """
    # 1. [目录准备] 确保上传目录存在
    target_dir = updated_dir / f"session_{thread_id}"
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    # 2. [保存] 遍历并写入文件
    for file in files:
        file_path = target_dir / file.filename
        # 使用二进制模式写入，支持各种文件格式 (图片、PDF、文本等)
        # shutil.copyfileobj 高效复制文件流，避免一次性加载大文件到内存
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        saved_files.append(file.filename)

    # 3. [响应] 返回成功保存的文件列表
    return {"status": "uploaded", "files": saved_files}


@app.get("/api/download")
async def download_file(path: str):
    """
    文件下载接口 (File Download)。

    目标：
    1. 根据绝对路径下载文件。
    2. 严格的安全检查，防止越权访问。

    Args:
        path (str): 文件的绝对路径 (通常从 list_files 接口获取)。
    """
    # 1. [安全检查] 路径解析与越权校验
    try:
        abs_path = Path(path).resolve()
        output_abs = output_dir.resolve()

        # 必须确保请求的文件在 output 目录下
        if not abs_path.is_relative_to(output_abs):
            return {"error": "拒绝访问: 只能下载输出目录下的文件"}
    except Exception:
        return {"error": "无效的路径参数"}
    # 2. [存在性检查]
    if not abs_path.exists():
        return {"error": "文件不存在"}

    # 3. [响应] 返回文件流 (浏览器自动触发下载)
    return FileResponse(abs_path, filename=abs_path.name)


@app.get("/api/files")
async def list_files(path: str = None):
    """
    文件列表查询接口 (File Explorer)。

    目标：
    1. 列出指定目录下的所有生成文件。
    2. 提供文件元数据（大小、时间、下载链接）。
    3. 严格的安全检查，防止路径遍历攻击。

    Args:
        path (str): 目标目录的绝对路径 (必须在 output 目录下)。不传则默认使用 output 目录。
    """
    # 默认使用 output 目录
    if path is None:
        path = str(output_dir)

    # 1. [调试] 打印请求路径
    print(f"[DEBUG] 请求文件列表: {path}")

    try:
        # 2. [解析] 获取绝对路径对象
        abs_path = Path(path).resolve()
        output_abs = output_dir.resolve()

        # 3. [安全] 检查路径是否越界 (Path Traversal Check)
        if not abs_path.is_relative_to(output_abs):
            print(f"[ERROR] 拒绝访问: {abs_path} 不在 {output_abs} 目录下")
            return {"error": "拒绝访问: 只能访问输出目录下的文件"}

    except Exception as e:
        print(f"[ERROR] 路径解析失败: {e}")
        return {"error": f"路径无效: {e}"}

    # 4. [检查] 目录是否存在
    if not abs_path.exists():
        return {"error": "目录不存在"}

    files = []
    try:
        # 5. [遍历] 递归查找所有文件
        for file_path in abs_path.rglob("*"):
            if file_path.is_file():
                # 计算相对路径，生成下载 URL
                stat = file_path.stat()
                files.append(
                    {
                        "name": file_path.name,
                        "type": "file",
                        "path": str(file_path),
                        # "url": f"/outputs/{url_path}",
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )

    except Exception as e:
        print(f"[ERROR] 遍历文件失败: {e}")
        return {"error": str(e)}

    # 6. [排序] 按修改时间倒序排列 (最新的在前)
    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    print(f"[DEBUG] 找到 {len(files)} 个文件")
    return {"files": files}


# 当浏览器请求 ws://localhost:8000/ws/thread_123 时：
# 1. 路由匹配 ：FastAPI 发现这个 URL 匹配了你写的 @app.websocket("/ws/{thread_id}") 。
# 2. 创建对象 ：FastAPI (基于 Starlette) 会立刻在 主事件循环 中实例化一个 WebSocket 对象。
#    - 这个对象封装了底层的 TCP 连接、HTTP 握手信息、以及后续的消息收发方法 ( send_text , receive_text 等)。
# 3. 注入参数 ：FastAPI 自动把这个刚创建好的 WebSocket 对象，作为参数传给你的 websocket_endpoint(websocket, ...) 函数。
@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    print(f"会话向我们发起了请求，要求简历连接：{thread_id} 对应：{websocket}")
    """
    WebSocket 实时通讯核心接口 (Real-time Communication)。

    目标：
    1. 建立长连接，实现服务端与前端的双向通信。
    2. 绑定 `thread_id`，实现会话级消息隔离。
    3. 维持心跳 (Keep-Alive)，防止连接超时。

    执行步骤：
    1. 握手：接受 WebSocket 连接请求。
    2. 注册：将连接实例绑定到 `monitor.manager`，关联 `thread_id`。
    3. 循环：进入消息监听循环，处理前端发送的心跳或指令。
    4. 异常：捕获断开连接异常，清理资源。

    Args:
        websocket (WebSocket): WebSocket 连接实例。
        thread_id (str): 当前会话的唯一标识。
    """
    # 1. [注册] 建立连接并绑定到管理器
    await manager.connect(websocket, thread_id)

    try:
        # 2. [循环] 保持连接活跃
        while True:
            # 3. [监听] 接收前端消息 (通常是 ping 心跳)
            data = await websocket.receive_text()

            # 4. [响应] 回复 pong 消息
            await websocket.send_json({"type": "pong", "message": f"服务端已收到: {data}"})

    except WebSocketDisconnect:
        # 5. [清理] 客户端主动断开
        manager.disconnect(websocket, thread_id)
        print(f"[WebSocket] 客户端已断开: {thread_id}")

    except Exception as e:
        # 6. [异常] 发生错误时断开
        print(f"[WebSocket] 连接异常: {e}")
        manager.disconnect(websocket, thread_id)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)

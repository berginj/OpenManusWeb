import asyncio
import json
import os
import platform
import re
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path
from typing import Dict, Literal
from urllib.parse import urlsplit

from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from app.config import config
from app.llm import LLM
from app.logger import logger
from app.schema import Message
from app.web import agent_monitor

from app.agent.manus import Manus
from app.flow.base import FlowType
from app.flow.flow_factory import FlowFactory
from app.web.log_handler import capture_session_logs, get_logs
from app.web.log_parser import get_all_logs_info, get_latest_log_info, parse_log_file
from app.web.thinking_tracker import ThinkingTracker


# 控制是否自动打开浏览器 (读取环境变量，默认为True)
AUTO_OPEN_BROWSER = os.environ.get("AUTO_OPEN_BROWSER", "1") == "1"
last_opened = False  # 跟踪浏览器是否已打开

app = FastAPI(title="OpenManus Web")

# 获取当前文件所在目录
current_dir = Path(__file__).parent
# 设置静态文件目录
app.mount("/static", StaticFiles(directory=current_dir / "static"), name="static")
# 设置模板目录
templates = Jinja2Templates(directory=current_dir / "templates")

# 存储活跃的会话及其结果
active_sessions: Dict[str, dict] = {}

# 存储任务取消事件
cancel_events: Dict[str, asyncio.Event] = {}

# 创建工作区根目录
WORKSPACE_ROOT = Path(__file__).parent.parent.parent / "workspace"
WORKSPACE_ROOT.mkdir(exist_ok=True)

# 日志目录
LOGS_DIR = Path(__file__).parent.parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# 导入日志监视器
from app.utils.log_monitor import LogFileMonitor


# 存储活跃的日志监视器
active_log_monitors: Dict[str, LogFileMonitor] = {}


# 创建工作区目录的函数
def create_workspace(session_id: str) -> Path:
    """为会话创建工作区目录"""
    # 简化session_id作为目录名
    job_id = f"job_{session_id[:8]}"
    workspace_dir = WORKSPACE_ROOT / job_id
    workspace_dir.mkdir(exist_ok=True)
    return workspace_dir


@app.on_event("startup")
async def startup_event():
    """启动事件：应用启动时自动打开浏览器"""
    global last_opened
    if AUTO_OPEN_BROWSER and not last_opened:
        # 延迟1秒以确保服务已经启动
        threading.Timer(1.0, lambda: webbrowser.open("http://localhost:8000")).start()
        print("Opening browser...")
        last_opened = True


class SessionRequest(BaseModel):
    prompt: str
    model: str | None = None
    workspace: str | None = None
    project_path: str | None = None
    previous_session_id: str | None = None
    execution_mode: Literal['auto', 'direct', 'plan'] = 'auto'


def runtime_environment():
    """Capture useful settings without retaining URL credentials, path or query."""
    settings = config.llm['default']
    try:
        parsed = urlsplit(settings.base_url)
        host = parsed.hostname
        if not parsed.scheme or not host:
            raise ValueError('Invalid endpoint')
        if ':' in host:
            host = f'[{host}]'
        origin = f'{parsed.scheme}://{host}' + (f':{parsed.port}' if parsed.port else '')
    except (ValueError, TypeError):
        origin = 'unavailable'
    return {'endpoint_origin': origin, 'api_type': settings.api_type,
            'max_output_tokens': settings.max_tokens, 'temperature': settings.temperature,
            'request_timeout_seconds': settings.request_timeout_seconds,
            'python_version': platform.python_version(), 'platform': platform.system(),
            'platform_release': platform.release()}


def previous_exchanges(session_id: str, workspace_dir: Path):
    """Load a small, explicitly linked conversation from the same workspace."""
    exchanges, seen = [], set()
    while session_id and len(exchanges) < 3:
        if session_id in seen:
            raise HTTPException(status_code=400, detail='Conversation history contains a cycle')
        seen.add(session_id)
        session = active_sessions.get(session_id) or agent_monitor.load_report(session_id)
        if not session:
            raise HTTPException(status_code=400, detail='Previous task was not found')
        if session.get('status') == 'processing':
            raise HTTPException(status_code=409, detail='Previous task is still running')
        previous_path = session.get('project_path') or (WORKSPACE_ROOT / (session.get('workspace') or ''))
        if Path(previous_path).resolve() != workspace_dir.resolve():
            raise HTTPException(status_code=400, detail='Previous task belongs to a different workspace')
        def excerpt(value, limit):
            text = str(value or '')
            if len(text) <= limit:
                return text
            first = limit // 2
            return text[:first] + '\n[Middle content truncated]\n' + text[-(limit - first):]
        exchanges.append({'session_id': session_id, 'status': session.get('status'),
                          'request': excerpt(session.get('prompt'), 2000),
                          'result': excerpt(session.get('result'), 4000)})
        session_id = session.get('previous_session_id')
    return list(reversed(exchanges))


def prompt_with_context(prompt: str, exchanges: list) -> str:
    if not exchanges:
        return prompt
    return ('Previous exchanges in this workspace (excerpts; inspect files when needed):\n'
            + json.dumps(exchanges, ensure_ascii=False, indent=2)
            + '\n\nCurrent user request:\n' + prompt)


@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    """主页入口 - 使用connected界面"""
    return HTMLResponse(
        content=open(
            current_dir / "static" / "connected_interface.html", encoding="utf-8"
        ).read()
    )


@app.get("/original", response_class=HTMLResponse)
async def get_original_interface(request: Request):
    """原始界面入口"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/connected", response_class=HTMLResponse)
async def get_connected_interface(request: Request):
    """连接后端的新界面入口 (与主页相同)"""
    return HTMLResponse(
        content=open(
            current_dir / "static" / "connected_interface.html", encoding="utf-8"
        ).read()
    )


@app.get("/api/agent-monitor")
async def agent_monitor_api(session_id: str = ""):
    details = await asyncio.to_thread(agent_monitor.ollama_models, config.llm['default'].base_url)
    run = agent_monitor.snapshot(session_id or None)
    report = agent_monitor.load_report(session_id) if session_id and run is None else None
    return {'default_model': config.llm['default'].model,
            'models': details['models'], 'loaded': details['loaded'],
            'loaded_details': details.get('loaded_details', []),
            'loaded_error': details.get('loaded_error'),
            'observed_at': details.get('observed_at'),
            'error': details.get('error'),
            'run': run or (report.get('metrics') if report else None),
            'progress': (report.get('progress') if report else ThinkingTracker.get_progress(session_id)) if session_id else None,
            'history': agent_monitor.history()}

@app.post("/api/chat")
async def create_chat_session(
    session_req: SessionRequest, background_tasks: BackgroundTasks
):
    # The original fork uses shared LLM singletons and process-global cwd.
    # To make per-task model selection safe, run one task at a time.
    if any(r.get('status') == 'processing' for r in agent_monitor.RUNS.values()):
        raise HTTPException(status_code=409, detail='Finish or stop the active task before starting another')
    chosen_model = session_req.model or config.llm['default'].model
    details = await asyncio.to_thread(agent_monitor.ollama_models, config.llm['default'].base_url)
    # The model discovery above yields to other requests, so recheck before reserving.
    if any(r.get('status') == 'processing' for r in agent_monitor.RUNS.values()):
        raise HTTPException(status_code=409, detail='A task is already running')
    if session_req.model and chosen_model not in details['models'] and chosen_model != config.llm['default'].model:
        if details.get('error'):
            raise HTTPException(status_code=503, detail='Cannot verify the selected model because Ollama is unreachable. Try again when it is online.')
        raise HTTPException(status_code=400, detail='Selected model is not in Ollama /api/tags')
    workspace_dir = None
    if session_req.project_path and session_req.workspace:
        raise HTTPException(status_code=400, detail='Choose a workspace or a project folder, not both')
    if session_req.project_path:
        requested = Path(session_req.project_path).expanduser()
        if not requested.is_absolute():
            raise HTTPException(status_code=400, detail='Project folder must be an absolute path')
        workspace_dir = requested.resolve()
        if not workspace_dir.is_dir():
            raise HTTPException(status_code=400, detail='Project folder does not exist')
    if session_req.workspace:
        name = session_req.workspace
        if not re.fullmatch(r'job_[A-Za-z0-9_-]+', name):
            raise HTTPException(status_code=400, detail='Invalid workspace name')
        workspace_dir = (WORKSPACE_ROOT / name).resolve()
        if workspace_dir.parent != WORKSPACE_ROOT.resolve() or not workspace_dir.is_dir():
            raise HTTPException(status_code=400, detail='Workspace does not exist')
    if session_req.previous_session_id and workspace_dir is None:
        raise HTTPException(status_code=400, detail='Choose the previous task workspace to continue it')
    exchanges = previous_exchanges(session_req.previous_session_id, workspace_dir) if session_req.previous_session_id else []
    session_id = str(uuid.uuid4())
    active_sessions[session_id] = {
        "status": "processing",
        "prompt": session_req.prompt,
        "result": None,
        "log": [],
        "workspace": None,
        "model": chosen_model,
        "requested_mode": session_req.execution_mode,
        "runtime_environment": runtime_environment(),
        "previous_session_id": session_req.previous_session_id,
        "context_exchanges": exchanges,
    }

    # 创建取消事件
    cancel_events[session_id] = asyncio.Event()

    # 创建工作区目录
    workspace_dir = workspace_dir or create_workspace(session_id)
    active_sessions[session_id]['workspace'] = (str(workspace_dir) if session_req.project_path
        else str(workspace_dir.relative_to(WORKSPACE_ROOT)))
    active_sessions[session_id]['project_path'] = str(workspace_dir) if session_req.project_path else None

    agent_monitor.begin_task(session_id, chosen_model)
    try:
        agent_monitor.save_report(session_id, await get_chat_diagnostics(session_id))
    except Exception as exc:
        print(f'Could not save initial diagnostic checkpoint: {exc}')
    background_tasks.add_task(process_prompt, session_id, session_req.prompt)
    return {
        "session_id": session_id,
        "workspace": active_sessions[session_id]["workspace"],
        "project_path": active_sessions[session_id]['project_path'],
        "context_count": len(exchanges),
    }


@app.get('/api/workspaces')
async def list_workspaces():
    root = WORKSPACE_ROOT.resolve()
    return {'workspaces': sorted(p.name for p in WORKSPACE_ROOT.glob('job_*')
            if p.is_dir() and p.resolve().parent == root)}


@app.get('/api/chat/{session_id}/project-files')
async def project_files(session_id: str, path: str = ''):
    session = active_sessions.get(session_id) or agent_monitor.load_report(session_id)
    if not session or not session.get('project_path'):
        raise HTTPException(status_code=404, detail='Project not found')
    root = Path(session['project_path']).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=403, detail='Access denied')
    if not target.exists():
        raise HTTPException(status_code=404, detail='File not found')
    if target.is_dir():
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        return {'path': path, 'entries': [{'name': p.name, 'directory': p.is_dir(),
                'path': str(p.relative_to(root))} for p in entries[:500]
                if p.resolve().is_relative_to(root)], 'truncated': len(entries) > 500}
    if not target.is_file() or target.stat().st_size > 1024 * 1024:
        raise HTTPException(status_code=400, detail='Only text files up to 1 MB can be viewed')
    try:
        return {'name': target.name, 'path': path, 'type': target.suffix.lstrip('.') or 'text',
                'content': target.read_text(encoding='utf-8')}
    except (UnicodeError, OSError):
        raise HTTPException(status_code=400, detail='File cannot be read as UTF-8 text')


@app.get("/api/chat/{session_id}")
async def get_chat_result(session_id: str):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    # 使用新的日志处理模块获取日志
    session = active_sessions[session_id]
    session["log"] = get_logs(session_id)

    return session


@app.get("/api/chat/{session_id}/diagnostics")
async def get_chat_diagnostics(session_id: str, refresh_model: bool = False):
    """Return a bounded, task-specific report suitable for copying to an agent."""
    session = active_sessions.get(session_id)
    if session is None:
        report = agent_monitor.load_report(session_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return report
    if refresh_model:
        await asyncio.to_thread(agent_monitor.ollama_models, config.llm['default'].base_url, True)
    def bounded(value, limit=2500):
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit] + f'\n[truncated: {len(value) - limit} characters omitted]'
        if isinstance(value, dict):
            return {key: bounded(item, limit) for key, item in value.items()}
        if isinstance(value, list):
            return [bounded(item, limit) for item in value]
        return value

    logs = get_logs(session_id)
    important = [entry for entry in logs if entry.get('level', '').upper() in ('WARNING', 'ERROR', 'CRITICAL')]
    return {
        "session_id": session_id,
        "prompt": bounded(session.get("prompt"), 8000),
        "status": session["status"],
        "result": session.get("result"),
        "error_phase": session.get('error_phase'),
        "error_traceback": session.get('error_traceback'),
        "workspace": session.get("workspace"),
        "project_path": session.get("project_path"),
        "requested_mode": session.get('requested_mode', 'auto'),
        "runtime_environment": session.get('runtime_environment'),
        "model_load": agent_monitor.cached_model_status(session.get('model'), config.llm['default'].base_url),
        "previous_session_id": session.get('previous_session_id'),
        "context_exchanges": session.get('context_exchanges', []),
        "metrics": agent_monitor.snapshot(session_id),
        "progress": ThinkingTracker.get_progress(session_id),
        "recent_steps": bounded(ThinkingTracker.get_thinking_steps(session_id)[-30:]),
        "recent_session_logs": bounded(logs[-30:]),
        "important_logs": bounded(important[-10:]),
        "note": "Recent events are limited to 30 per source, plus up to 10 warnings and errors; long event fields are truncated. Review for sensitive information before sharing.",
    }


@app.get('/api/reports')
async def list_diagnostic_reports(limit: int = Query(50, ge=1, le=100),
                                  before_time: float | None = Query(None, ge=0),
                                  before_id: str | None = Query(None, max_length=128),
                                  q: str = Query('', max_length=120)):
    if (before_time is None) != (before_id is None):
        raise HTTPException(status_code=400, detail='Both cursor fields are required')
    return agent_monitor.recent_reports(limit, before_time, before_id, q)


@app.post("/api/chat/{session_id}/stop")
async def stop_processing(session_id: str):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    if active_sessions[session_id]['status'] != 'processing':
        return {'status': active_sessions[session_id]['status']}
    if session_id in cancel_events:
        cancel_events[session_id].set()
    active_sessions[session_id]['stop_requested'] = True
    ThinkingTracker.add_thinking_step(session_id, 'Stop requested; canceling model requests and waiting for any running tool')
    return {'status': 'stopping'}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    try:
        await websocket.accept()

        if session_id not in active_sessions:
            await websocket.send_text(json.dumps({"error": "Session not found"}))
            await websocket.close()
            return

        session = active_sessions[session_id]

        # 注册 WebSocket 发送回调函数
        async def ws_send(message: str):
            try:
                await websocket.send_text(message)
            except Exception as e:
                print(f"WebSocket 发送消息失败: {str(e)}")

        ThinkingTracker.register_ws_send_callback(session_id, ws_send)

        initial_steps = ThinkingTracker.get_thinking_steps(session_id)
        initial_logs = get_logs(session_id)
        initial_session_logs = session['log'][:]
        # 初始状态通知中添加日志信息
        await websocket.send_text(
            json.dumps(
                {
                    "status": session["status"],
                    "result": session["result"],
                    "log": initial_session_logs,
                    "thinking_steps": initial_steps,
                    "logs": initial_logs,
                }
            )
        )

        # 获取工作区名称(job_id) - 优先从环境变量获取
        job_id = f'job_{session_id[:8]}'

        # 如果当前没有日志监控器，则创建一个
        if session_id not in active_log_monitors and job_id:
            log_path = LOGS_DIR / f"{job_id}.log"
            if log_path.exists():
                log_monitor = LogFileMonitor(job_id)
                log_monitor.start_monitoring()
                active_log_monitors[session_id] = log_monitor

        # 跟踪日志更新
        last_log_entries = []
        if job_id and session_id in active_log_monitors:
            last_log_entries = active_log_monitors[session_id].get_log_entries()

        # 等待结果更新
        last_log_count = len(initial_session_logs)
        last_thinking_step_count = len(initial_steps)
        last_tracker_log_count = len(initial_logs)
        result_sent = session['status'] != 'processing'

        while session["status"] == "processing":
            await asyncio.sleep(0.2)  # 降低检查间隔提高实时性

            # 检查系统日志更新 (新增)
            if job_id and session_id in active_log_monitors:
                current_log_entries = active_log_monitors[session_id].get_log_entries()
                if len(current_log_entries) > len(last_log_entries):
                    new_logs = current_log_entries[len(last_log_entries) :]
                    await websocket.send_text(
                        json.dumps(
                            {
                                "status": session["status"],
                                "system_logs": new_logs,
                                # 添加一个chat_logs字段，将系统日志作为聊天消息发送
                                "chat_logs": new_logs,
                            }
                        )
                    )
                    last_log_entries = current_log_entries

            # 检查日志更新
            current_log_count = len(session["log"])
            if current_log_count > last_log_count:
                await websocket.send_text(
                    json.dumps(
                        {
                            "status": session["status"],
                            "log": session["log"][last_log_count:],
                        }
                    )
                )
                last_log_count = current_log_count

            # 检查思考步骤更新
            thinking_steps = ThinkingTracker.get_thinking_steps(session_id)
            current_thinking_step_count = len(thinking_steps)
            if current_thinking_step_count > last_thinking_step_count:
                await websocket.send_text(
                    json.dumps(
                        {
                            "status": session["status"],
                            "thinking_steps": thinking_steps[last_thinking_step_count:],
                        }
                    )
                )
                last_thinking_step_count = current_thinking_step_count

            # 检查ThinkingTracker日志更新
            tracker_logs = get_logs(session_id)
            current_tracker_log_count = len(tracker_logs)
            if current_tracker_log_count > last_tracker_log_count:
                await websocket.send_text(
                    json.dumps(
                        {
                            "status": session["status"],
                            "logs": tracker_logs[last_tracker_log_count:],
                        }
                    )
                )
                last_tracker_log_count = current_tracker_log_count

            # 检查结果更新
            if session["result"]:
                await websocket.send_text(
                    json.dumps(
                        {
                            "status": session["status"],
                            "result": session["result"],
                            "log": session["log"][last_log_count:],
                            "thinking_steps": ThinkingTracker.get_thinking_steps(
                                session_id, last_thinking_step_count
                            ),
                            "system_logs": last_log_entries,  # 添加系统日志
                            "logs": get_logs(session_id)[last_tracker_log_count:],
                        }
                    )
                )
                result_sent = True
                break  # 结果已发送，退出循环，避免重复发送

        # 仅在循环没有因result而break时才发送最终结果
        if not result_sent:
            await websocket.send_text(
                json.dumps(
                    {
                        "status": session["status"],
                        "result": session["result"],
                        "log": session["log"][last_log_count:],
                        "thinking_steps": ThinkingTracker.get_thinking_steps(
                            session_id, last_thinking_step_count
                        ),
                        "system_logs": last_log_entries,  # 添加系统日志
                        "logs": get_logs(session_id)[last_tracker_log_count:],
                    }
                )
            )

        # 取消注册 WebSocket 发送回调函数
        ThinkingTracker.unregister_ws_send_callback(session_id)
        await websocket.close()
    except WebSocketDisconnect:
        # 客户端断开连接，正常操作
        ThinkingTracker.unregister_ws_send_callback(session_id)
    except Exception as e:
        # 其他异常，记录日志但不中断应用
        print(f"WebSocket错误: {str(e)}")
        ThinkingTracker.unregister_ws_send_callback(session_id)


# 在适当位置添加LLM通信钩子
from app.web.thinking_tracker import ThinkingTracker


# 修改通信跟踪器的实现方式
class LLMCommunicationTracker:
    """跟踪与LLM的通信内容，使用monkey patching代替回调"""

    def __init__(self, session_id: str, agent=None):
        self.session_id = session_id
        self.agent = agent
        self.original_run_method = None

        # 如果提供了agent，安装钩子
        if agent and hasattr(agent, "llm") and hasattr(agent.llm, "completion"):
            self.install_hooks()

    def install_hooks(self):
        """安装钩子以捕获LLM通信内容"""
        if not self.agent or not hasattr(self.agent, "llm"):
            return False

        # 保存原始方法
        llm = self.agent.llm
        if hasattr(llm, "completion"):
            self.original_completion = llm.completion
            # 替换为我们的包装方法
            llm.completion = self._wrap_completion(self.original_completion)
            return True
        return False

    def uninstall_hooks(self):
        """卸载钩子，恢复原始方法"""
        if self.agent and hasattr(self.agent, "llm") and self.original_completion:
            self.agent.llm.completion = self.original_completion

    def _wrap_completion(self, original_method):
        """包装LLM的completion方法以捕获输入和输出"""
        session_id = self.session_id

        async def wrapped_completion(*args, **kwargs):
            # 记录输入
            prompt = kwargs.get("prompt", "")
            if not prompt and args:
                prompt = args[0]
            if prompt:
                ThinkingTracker.add_communication(
                    session_id,
                    "Sent to LLM",
                    prompt[:500] + ("..." if len(prompt) > 500 else ""),
                )

            # 调用原始方法
            result = await original_method(*args, **kwargs)

            # 记录输出
            if result:
                content = result
                if isinstance(result, dict) and "content" in result:
                    content = result["content"]
                elif hasattr(result, "content"):
                    content = result.content

                if isinstance(content, str):
                    ThinkingTracker.add_communication(
                        session_id,
                        "Received from LLM",
                        content[:500] + ("..." if len(content) > 500 else ""),
                    )

            return result

        return wrapped_completion


# 导入新创建的LLM包装器
from app.agent.llm_wrapper import LLMCallbackWrapper


# 修改文件API，支持工作区目录
@app.get("/api/files")
async def get_generated_files():
    """获取所有工作区目录和文件"""
    result = []

    # 获取所有工作区目录
    workspaces = list(WORKSPACE_ROOT.glob("job_*"))
    workspaces.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for workspace in workspaces:
        workspace_name = workspace.name
        # 获取工作区内所有文件并按修改时间排序
        files = []
        with os.scandir(workspace) as it:
            for entry in it:
                if entry.is_file() and entry.name.split(".")[-1] in [
                    "txt",
                    "md",
                    "html",
                    "css",
                    "js",
                    "py",
                    "json",
                ]:
                    files.append(entry)
        # 按修改时间倒序排序
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

        # 如果有文件，添加该工作区
        if files:
            workspace_item = {
                "name": workspace_name,
                "path": str(workspace.relative_to(Path(__file__).parent.parent.parent)),
                "modified": workspace.stat().st_mtime,
                "files": [],
            }

            # 添加工作区下的文件
            for file in sorted(files, key=lambda p: p.name):
                workspace_item["files"].append(
                    {
                        "name": file.name,
                        "path": str(
                            Path(file.path).relative_to(
                                Path(__file__).parent.parent.parent
                            )
                        ),
                        "type": Path(file.path).suffix[1:],  # 去掉.的扩展名
                        "size": file.stat().st_size,
                        "modified": file.stat().st_mtime,
                    }
                )

            result.append(workspace_item)

    return {"workspaces": result}


# 新增日志文件接口
@app.get("/api/logs")
async def get_system_logs(limit: int = 10):
    """获取系统日志列表"""
    log_files = []
    for entry in os.scandir(LOGS_DIR):
        if entry.is_file() and entry.name.endswith(".log"):
            log_files.append(
                {
                    "name": entry.name,
                    "size": entry.stat().st_size,
                    "modified": entry.stat().st_mtime,
                }
            )
    # 按修改时间倒序排序并限制数量
    log_files.sort(key=lambda x: x["modified"], reverse=True)
    return {"logs": log_files[:limit]}


@app.get("/api/logs/{log_name}")
async def get_log_content(log_name: str, parsed: bool = False):
    """获取特定日志文件内容"""
    log_path = LOGS_DIR / log_name
    # 安全检查
    if not log_path.exists() or not log_path.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")

    # 如果请求解析后的日志信息
    if parsed:
        log_info = parse_log_file(str(log_path))
        log_info["name"] = log_name
        return log_info

    # 否则返回原始内容
    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()

    return {"name": log_name, "content": content}


@app.get("/api/logs_parsed")
async def get_parsed_logs(limit: int = 10):
    """获取解析后的日志信息列表"""
    return {"logs": get_all_logs_info(str(LOGS_DIR), limit)}


@app.get("/api/logs_parsed/{log_name}")
async def get_parsed_log(log_name: str):
    """获取特定日志文件的解析信息"""
    log_path = LOGS_DIR / log_name
    # 安全检查
    if not log_path.exists() or not log_path.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")

    log_info = parse_log_file(str(log_path))
    log_info["name"] = log_name
    return log_info


@app.get("/api/latest_log")
async def get_latest_log():
    """获取最新日志文件的解析信息"""
    return get_latest_log_info(str(LOGS_DIR))


@app.get("/api/files/{file_path:path}")
async def get_file_content(file_path: str):
    """获取特定文件的内容"""
    # 安全检查，防止目录遍历攻击
    root_dir = Path(__file__).parent.parent.parent
    full_path = (root_dir / file_path).resolve()

    # 确保文件在项目目录内
    try:
        full_path.relative_to(root_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # 读取文件内容
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 确定文件类型
        file_type = full_path.suffix[1:] if full_path.suffix else "text"

        return {
            "name": full_path.name,
            "path": file_path,
            "type": file_type,
            "content": content,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")


# 修改process_prompt函数，处理工作区
async def review_task(prompt: str, exchanges=None):
    """Review scope before choosing execution mode; keep obvious greetings cheap."""
    if re.fullmatch(r'\s*(hi|hello|hey|thanks|thank you)[!.?\s]*', prompt, re.I):
        return 'simple', 'Brief greeting; direct execution.'
    number = r'[+-]?\d+(?:\.\d+)?'
    if re.fullmatch(rf'\s*(?:(?:what\s+is|calculate|compute|evaluate)\s+)?{number}\s*(?:[+*/×÷-]|plus|minus|times|divided\s+by)\s*{number}\s*[?.!]?\s*', prompt, re.I):
        return 'simple', 'Single arithmetic expression; direct execution.'
    try:
        response = await asyncio.wait_for(LLM().ask(
            messages=[Message.user_message(prompt_with_context(prompt, exchanges or []))],
            system_msgs=[Message.system_message(
                'Review the user task for execution, without carrying it out. '
                'Reply with exactly SIMPLE or COMPLEX on the first line, then one short '
                'sentence describing the scope. SIMPLE means one straightforward action '
                'or answer needing no multi-step plan; otherwise choose COMPLEX.')],
            stream=False, temperature=0), timeout=120)
        first = response.strip().splitlines()[0].strip().upper().strip(' .:')
        if first in ('SIMPLE', 'COMPLEX'):
            return first.lower(), response.strip()[:1000]
    except Exception as exc:
        return 'complex', f'Review unavailable ({type(exc).__name__}); using planned execution.'
    return 'complex', 'Review was inconclusive; using planned execution.'


async def choose_execution(prompt: str, exchanges: list, mode: str):
    if mode == 'direct':
        return 'direct', 'Direct execution selected; automatic review skipped.'
    complexity, review = await review_task(prompt, exchanges)
    if mode == 'plan':
        return 'planned', f'Planning selected by user. Review: {review}'
    return complexity, review


def record_task_failure(session_id: str, exc: Exception, phase: str):
    """Keep a bounded traceback alongside the short message shown in chat."""
    trace = traceback.format_exc()
    if len(trace) > 12000:
        omitted = len(trace) - 12000
        trace = trace[:6000] + f'\n[{omitted} characters omitted]\n' + trace[-6000:]
    session = active_sessions[session_id]
    session['status'] = 'error'
    session['result'] = f'{phase}: {exc}'
    session['error_phase'] = phase
    session['error_traceback'] = trace
    if ThinkingTracker.get_status(session_id) == 'pending':
        ThinkingTracker.start_tracking(session_id)
    ThinkingTracker.add_error(session_id, f'{phase}: {exc}')
    logger.error('{}\n{}', session['result'], trace)


async def process_prompt(session_id: str, prompt: str):
    """Ensure setup failures do not leave a task permanently marked as running."""
    original_cwd = os.getcwd()
    try:
        await _process_prompt(session_id, prompt)
    except Exception as exc:
        os.chdir(original_cwd)
        record_task_failure(session_id, exc, 'Task setup or cleanup failed')
        agent_monitor.finish_task(session_id, 'error')
        cancel_events.pop(session_id, None)
        try:
            agent_monitor.save_report(session_id, await get_chat_diagnostics(session_id))
        except Exception as report_exc:
            print(f'Could not save diagnostic report: {report_exc}')


async def _process_prompt(session_id: str, prompt: str):
    monitor_token = agent_monitor.enter_task(session_id, cancel_events.get(session_id))
    # Apply the selected model BEFORE agent/flow initialization.
    selected_model = active_sessions[session_id]['model']
    config.llm['default'].model = selected_model
    for instance in LLM._instances.values():
        if instance.base_url == config.llm['default'].base_url:
            instance.model = selected_model
    # 获取会话工作区
    workspace_dir = None
    if session_id in active_sessions and "workspace" in active_sessions[session_id]:
        workspace_path = active_sessions[session_id]["workspace"]
        workspace_dir = Path(workspace_path) if active_sessions[session_id].get('project_path') else WORKSPACE_ROOT / workspace_path

    # 如果没有工作区，创建一个
    if not workspace_dir:
        workspace_dir = create_workspace(session_id)
        if session_id in active_sessions:
            active_sessions[session_id]["workspace"] = str(
                workspace_dir.relative_to(WORKSPACE_ROOT)
            )

    # 设置当前工作目录为工作区
    original_cwd = os.getcwd()
    os.chdir(workspace_dir)

    # 使用工作区名称作为日志文件名前缀
    job_id = f'job_{session_id[:8]}'
    # 设置日志文件路径
    task_log_path = LOGS_DIR / f"{job_id}.log"

    # 创建日志监视器并开始监控
    log_monitor = LogFileMonitor(job_id, log_dir=str(LOGS_DIR))
    observer = log_monitor.start_monitoring()
    active_log_monitors[session_id] = log_monitor

    async def sync_logs():
        """定期从LogFileMonitor获取日志并实时更新到ThinkingTracker"""
        last_count = 0
        next_checkpoint = time.monotonic() + 5
        try:
            while True:
                if session_id not in active_log_monitors:
                    break
                current_logs = get_logs(session_id)
                if len(current_logs) > last_count:
                    # 处理新的日志条目
                    new_logs = current_logs[last_count:]
                    # 逐条处理每条新日志，确保实时性
                    for log_entry in new_logs:
                        # 单独处理每条日志，立即添加到ThinkingTracker
                        if isinstance(log_entry, str):
                            log_entry = {'message': log_entry}
                        ThinkingTracker.add_log_entry(
                            session_id,
                            {
                                "level": log_entry.get("level", "INFO"),
                                "message": log_entry.get("message", ""),
                                "timestamp": log_entry.get("timestamp", time.time()),
                            },
                        )
                    last_count = len(current_logs)
                if time.monotonic() >= next_checkpoint:
                    try:
                        agent_monitor.save_report(session_id, await get_chat_diagnostics(session_id))
                    except Exception as exc:
                        print(f'Could not save diagnostic checkpoint: {exc}')
                    next_checkpoint = time.monotonic() + 5
                # 减少轮询间隔，提高实时性
                await asyncio.sleep(0.1)  # 每0.1秒检查一次
        except Exception as e:
            print(f"同步日志时发生错误: {str(e)}")

    # 启动日志同步任务
    sync_task = asyncio.create_task(sync_logs())

    # 设置环境变量告知logger使用此日志文件，确保两种方式都设置
    os.environ["OPENMANUS_LOG_FILE"] = str(task_log_path)
    os.environ["OPENMANUS_TASK_ID"] = job_id

    task_log_sink = None
    try:
        task_log_sink = logger.add(str(task_log_path), rotation="100 MB")
        # 使用日志捕获上下文管理器解析日志级别和内容
        with capture_session_logs(session_id) as log:
            # 初始化思考跟踪
            ThinkingTracker.start_tracking(session_id)
            ThinkingTracker.add_thinking_step(session_id, "Starting user request")
            ThinkingTracker.add_thinking_step(
                session_id, f"Workspace: {workspace_dir.name}"
            )

            # 直接记录用户输入的prompt
            ThinkingTracker.add_communication(session_id, "User input", prompt)

            # 初始化代理和任务流程
            ThinkingTracker.add_thinking_step(session_id, "Initializing AI agent and task flow")
            agent = Manus()

            # 使用包装器包装LLM
            if hasattr(agent, "llm"):
                original_llm = agent.llm
                wrapped_llm = LLMCallbackWrapper(original_llm)

                # 注册回调函数
                def on_before_request(data):
                    # 提取请求内容
                    prompt_content = None
                    if data.get("args") and len(data["args"]) > 0:
                        prompt_content = str(data["args"][0])
                    elif data.get("kwargs") and "prompt" in data["kwargs"]:
                        prompt_content = data["kwargs"]["prompt"]
                    else:
                        prompt_content = str(data)

                    # 记录通信内容
                    print(f"发送到LLM: {prompt_content[:100]}...")
                    ThinkingTracker.add_communication(
                        session_id, "Sent to LLM", prompt_content
                    )

                def on_after_request(data):
                    # 提取响应内容
                    response = data.get("response", "")
                    response_content = ""

                    # 尝试从不同格式中提取文本内容
                    if isinstance(response, str):
                        response_content = response
                    elif isinstance(response, dict):
                        if "content" in response:
                            response_content = response["content"]
                        elif "text" in response:
                            response_content = response["text"]
                        else:
                            response_content = str(response)
                    elif hasattr(response, "content"):
                        response_content = response.content
                    else:
                        response_content = str(response)

                    # 记录通信内容
                    print(f"从LLM接收: {response_content[:100]}...")
                    ThinkingTracker.add_communication(
                        session_id, "Received from LLM", response_content
                    )

                # 注册回调
                wrapped_llm.register_callback("before_request", on_before_request)
                wrapped_llm.register_callback("after_request", on_after_request)

                # 替换原始LLM
                agent.llm = wrapped_llm

            exchanges = active_sessions[session_id].get('context_exchanges', [])
            mode = active_sessions[session_id].get('requested_mode', 'auto')
            ThinkingTracker.add_thinking_step(session_id, "Direct execution selected" if mode == 'direct' else "Reviewing task scope")
            complexity, review = await choose_execution(prompt, exchanges, mode)
            agent_monitor.set_review(session_id, complexity, review, prompt)
            ThinkingTracker.add_thinking_step(session_id, f"Task review ({complexity}): {review}")
            if exchanges:
                ThinkingTracker.add_thinking_step(session_id, f"Including excerpts from {len(exchanges)} previous task(s) in this workspace")

            # 记录处理开始
            ThinkingTracker.add_thinking_step(
                session_id, f"Analyzing user request: {prompt[:50]}{'...' if len(prompt) > 50 else ''}"
            )
            log.info(f"Starting task: {prompt[:50]}{'...' if len(prompt) > 50 else ''}")

            # 检查任务是否被取消
            cancel_event = cancel_events.get(session_id)
            if cancel_event and cancel_event.is_set():
                log.warning("Processing canceled by user")
                ThinkingTracker.mark_stopped(session_id)
                active_sessions[session_id]["status"] = "stopped"
                active_sessions[session_id]["result"] = "Processing stopped by user"
                return

            # 执行前检查工作区已有文件
            existing_files = set()
            for ext in ["*.txt", "*.md", "*.html", "*.css", "*.js", "*.py", "*.json"]:
                existing_files.update(f.name for f in workspace_dir.glob(ext))

            # 跟踪计划创建过程
            if complexity in ('complex', 'planned'):
                ThinkingTracker.add_thinking_step(session_id, "Creating task execution plan")

            # 获取取消事件以传递给flow.execute
            cancel_event = cancel_events.get(session_id)

            # 初始检查，如果已经取消则不执行
            if cancel_event and cancel_event.is_set():
                log.warning("Processing canceled by user")
                ThinkingTracker.mark_stopped(session_id)
                active_sessions[session_id]["status"] = "stopped"
                active_sessions[session_id]["result"] = "Processing stopped by user"
                return

            # 执行实际处理 - 传递job_id和cancel_event给flow.execute方法
            if complexity in ('simple', 'direct'):
                ThinkingTracker.add_thinking_step(session_id, "Executing directly")
                agent.finish_on_text = True
                result = await agent.run(prompt_with_context(prompt, exchanges), cancel_event=cancel_event)
            else:
                flow = FlowFactory.create_flow(flow_type=FlowType.PLANNING, agents=agent)
                result = await flow.execute(prompt_with_context(prompt, exchanges), job_id, cancel_event)

            if cancel_event and cancel_event.is_set():
                ThinkingTracker.mark_stopped(session_id)
                active_sessions[session_id]['status'] = 'stopped'
                active_sessions[session_id]['result'] = 'Processing stopped by user'
                return

            # 执行结束后检查新生成的文件
            new_files = set()
            for ext in ["*.txt", "*.md", "*.html", "*.css", "*.js", "*.py", "*.json"]:
                new_files.update(f.name for f in workspace_dir.glob(ext))
            newly_created = new_files - existing_files

            if newly_created:
                files_list = ", ".join(newly_created)
                ThinkingTracker.add_thinking_step(
                    session_id,
                    f"Generated {len(newly_created)} files in workspace {workspace_dir.name}: {files_list}",
                )
                # 将文件列表也添加到会话结果中
                active_sessions[session_id]["generated_files"] = list(newly_created)

            # 记录完成情况
            log.info("Processing completed")
            ThinkingTracker.add_conclusion(
                session_id, f"Task completed! Results saved to workspace {workspace_dir.name}."
            )

            active_sessions[session_id]["status"] = "completed"
            active_sessions[session_id]["result"] = result
            active_sessions[session_id][
                "thinking_steps"
            ] = ThinkingTracker.get_thinking_steps(session_id)

    except asyncio.CancelledError:
        # 处理取消情况
        print("处理已取消")
        ThinkingTracker.mark_stopped(session_id)
        active_sessions[session_id]["status"] = "stopped"
        active_sessions[session_id]["result"] = "Processing canceled"
    except Exception as e:
        # 处理错误情况
        record_task_failure(session_id, e, 'Processing error')
    finally:
        if task_log_sink is not None:
            logger.remove(task_log_sink)
        agent_monitor.finish_task(session_id, active_sessions.get(session_id, {}).get('status', 'error'))
        try:
            agent_monitor.save_report(session_id, await get_chat_diagnostics(session_id))
        except Exception as exc:
            print(f'Could not save diagnostic report: {exc}')
        agent_monitor.leave_task(monitor_token)
        # 恢复原始工作目录
        os.chdir(original_cwd)

        # 清除日志文件环境变量
        if "OPENMANUS_LOG_FILE" in os.environ:
            del os.environ["OPENMANUS_LOG_FILE"]
        if "OPENMANUS_TASK_ID" in os.environ:
            del os.environ["OPENMANUS_TASK_ID"]

        # 清理资源
        if (
            "agent" in locals()
            and hasattr(agent, "llm")
            and isinstance(agent.llm, LLMCallbackWrapper)
        ):
            try:
                # 正确地移除回调
                if "on_before_request" in locals():
                    agent.llm._callbacks["before_request"].remove(on_before_request)
                if "on_after_request" in locals():
                    agent.llm._callbacks["after_request"].remove(on_after_request)
            except (ValueError, Exception) as e:
                print(f"清理回调时出错: {str(e)}")

        # 清理取消事件
        if session_id in cancel_events:
            del cancel_events[session_id]

        # 如果监视器存在，停止监控
        if session_id in active_log_monitors:
            observer.stop()
            observer.join(timeout=1)
            del active_log_monitors[session_id]

        # 取消日志同步任务
        if sync_task:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass


# 添加一个新的API端点来获取思考步骤
@app.get("/api/thinking/{session_id}")
async def get_thinking_steps(session_id: str, start_index: int = 0):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "status": ThinkingTracker.get_status(session_id),
        "thinking_steps": ThinkingTracker.get_thinking_steps(session_id, start_index),
    }


# 添加获取进度信息的API端点
@app.get("/api/progress/{session_id}")
async def get_progress(session_id: str):
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    return ThinkingTracker.get_progress(session_id)


# 添加API端点获取指定会话的系统日志
@app.get("/api/systemlogs/{session_id}")
async def get_system_logs(session_id: str):
    """获取指定会话的系统日志"""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    job_id = f'job_{session_id[:8]}'

    if not job_id:
        return {"logs": []}

    # 如果有监控器使用监控器
    if session_id in active_log_monitors:
        logs = active_log_monitors[session_id].get_log_entries()
        return {"logs": logs}

    # 否则直接读取日志文件
    log_path = LOGS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return {"logs": []}

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            logs = [line.strip() for line in f.readlines()]
        return {"logs": logs}
    except Exception as e:
        return {"error": f"Error reading log file: {str(e)}"}

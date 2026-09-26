import datetime
import asyncio
from typing import Any, Dict, Optional
from fastapi import WebSocket
from api.context import get_run_context, get_thread_context
from loguru import logger

# 尝试导入全局运行时（用于脚本模式下的流式输出）
try:
    import builtins
except ImportError:
    builtins = None

# 工具监控类，用于在工具执行过程中上报进度和状态
class ToolMonitor:

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToolMonitor, cls).__new__(cls)
            cls._instance.websocket_manager = None  # 预留给 FastAPI WebSocketManager
            cls._instance.event_publisher = None
            cls._instance.event_loop = None
            cls._instance.pending_events = set()
        return cls._instance

    def set_websocket_manager(self, manager):
        """设置 FastAPI 的 WebSocket 管理器"""
        self.websocket_manager = manager

    def set_event_publisher(self, publisher, loop=None):
        """Bind durable event publishing to the application event loop."""
        self.event_publisher = publisher
        self.event_loop = loop

    async def _publish_safely(self, publisher, **event) -> None:
        try:
            await publisher.publish(**event)
        except Exception as error:
            logger.error(f"[Monitor] Event persistence failed: {error}")

    async def drain(self) -> None:
        """Wait until events already scheduled for persistence are finished."""
        self.pending_events.discard(None)
        while self.pending_events:
            pending = tuple(self.pending_events)
            awaitables = [
                future
                if isinstance(future, asyncio.Future)
                else asyncio.wrap_future(future)
                for future in pending
            ]
            await asyncio.gather(*awaitables)

    def _emit(self, event_type: str, message: str, data: Optional[Dict[str, Any]] = None):
        """内部发送方法"""
        payload = {
            "type": "monitor_event",
            "event": event_type,
            "message": message,
            "data": data or {},
            "timestamp": datetime.datetime.now().isoformat()
        }

        # 1. 先持久化，再由发布器定向广播。
        publisher = self.event_publisher
        if publisher and self.event_loop:
            try:
                thread_id = get_thread_context()
                if thread_id:
                    event = {
                        "thread_id": thread_id,
                        "run_id": get_run_context(),
                        "event_type": event_type,
                        "message": message,
                        "data": data or {},
                    }
                    try:
                        current_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        current_loop = None
                    coroutine = self._publish_safely(publisher, **event)
                    if current_loop and current_loop == self.event_loop:
                        scheduled = current_loop.create_task(coroutine)
                    else:
                        scheduled = asyncio.run_coroutine_threadsafe(
                            coroutine,
                            self.event_loop,
                        )
                    if scheduled is not None:
                        self.pending_events.add(scheduled)
                        scheduled.add_done_callback(self.pending_events.discard)
            except Exception as error:
                logger.error(f"[Monitor] Event scheduling failed: {error}")

        # 2. 尝试通过全局 runtime 输出 (DeepAgents 脚本模式)
        # 这使得 simple_agents.py 中的 MockRuntime 能接收到数据
        if builtins and hasattr(builtins, 'runtime') and hasattr(builtins.runtime, 'stream_writer'):
            try:
                builtins.runtime.stream_writer(payload)
            except Exception:
                pass

        # 3. 控制台保底输出 (方便调试)
        # 加上特殊前缀，方便肉眼识别
        logger.info(f"\n[Monitor:{event_type}] {message}")

    def report_tool(self, tool_name: str, args: Dict[str, Any] = None):
        """报告工具开始执行"""
        self._emit("tool_start", f"开始执行工具: {tool_name}", {"tool_name": tool_name, "args": args})

    def report_assistant(self, assistant_name: str, args: Dict[str, Any] = None):
        """报告正在调用的子智能体进度"""
        self._emit("assistant_call", f"正在调用助手: {assistant_name}",
                   {"assistant_name": assistant_name, "args": args})

    def report_task_result(self, result: str):
        """报告任务最终结果"""
        self._emit("task_result", "任务执行完成", {"result": result})

    def report_session_dir(self, path: str):
        """报告任务工作目录"""
        self._emit("session_created", f"工作目录已创建: {path}", {"path": path})


# 全局单例实例
monitor = ToolMonitor()


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, set[WebSocket]] = {}
        # 延迟绑定 loop，防止初始化时 loop 不一致
        self.loop = None

    def get_loop(self):
        """懒加载获取当前运行的事件循环"""
        if self.loop is None:
            try:
                self.loop = asyncio.get_running_loop()
                # 同时设置 monitor 的 manager (确保双向绑定)
                monitor.set_websocket_manager(self)
                logger.info(f"[Monitor] ConnectionManager auto-bound to loop: {id(self.loop)}")
            except RuntimeError:
                logger.error("[Monitor] Warning: No running event loop found yet.")
        return self.loop

    async def connect(self, websocket: WebSocket, thread_id: str):
        # TestClient、服务重载等场景可能更换事件循环，因此每次连接都重新绑定。
        self.loop = asyncio.get_running_loop()
        monitor.set_websocket_manager(self)

        await websocket.accept()
        self.active_connections.setdefault(thread_id, set()).add(websocket)
        logger.info(f"Client connected: {thread_id}")

    def disconnect(self, websocket: WebSocket, thread_id: str):
        connections = self.active_connections.get(thread_id)
        if connections is not None:
            connections.discard(websocket)
            if not connections:
                del self.active_connections[thread_id]
        logger.info(f"Client disconnected: {thread_id}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    async def send_to_thread(self, message: dict, thread_id: str):
        for websocket in tuple(self.active_connections.get(thread_id, ())):
            try:
                await websocket.send_json(message)
            except Exception as error:
                logger.warning(f"WebSocket send failed for {thread_id}: {error}")
                self.disconnect(websocket, thread_id)


manager = ConnectionManager()

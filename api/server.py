import sys
import uuid
import asyncio
from csv import excel

import uvicorn
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import shutil
from loguru import logger

# 配置项目路径到环境变量
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

# 导入agent已经monitor
from agent.main_agent import run_deep_agent
from api.monitor import monitor,manager

app = FastAPI(title="DeepAgents API")

# 挂载输出目录，以便前端访问文件
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

# 定义上传目录 uploaded
upload_dir = project_root / "upload"
upload_dir.mkdir(exist_ok=True)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TaskRequest(BaseModel):
    query: str
    thread_id: str | None = None


# 开启任务接口实现
@app.post("/api/task")
async def run_task(request: TaskRequest):
    """
    智能体任务启动接口 (Run Agent Task)。

    目标：
    1. 接收用户的自然语言指令。
    2. 在后台异步启动 Agent 执行逻辑。
    3. 返回会话 ID，供前端通过 WebSocket 订阅实时进度。

    执行步骤：
    1. 获取或生成 thread_id。
    2. 触发异步任务 (asyncio.create_task)。
    3. 立即返回响应，不阻塞 HTTP 线程。

    Args:
        request (TaskRequest): 包含用户 query 和可选 thread_id 的请求体。
    """
    # 1. ID 初始化
    thread_id = request.thread_id or str(uuid.uuid4())
    # 2. 后台异步执行 Agent
    asyncio.create_task(run_deep_agent(request.query, thread_id))
    # result = await run_deep_agent(request.query, thread_id)
    # bg.add_task(run_deep_agent_safe, request.query, thread_id) 后续看一下
    return {"status":"started", "thread_id":thread_id}


# 上传文件接口
@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...),thread_id: str = Form(...)):
    """
    文件上传接口

    目标：
    1. 接收用户上传的一个或多个文件
    2. 保存到'updated/session_dir{thread_id}'目录
    3. 供Agent在后续任务中读取和分析

    Args:
    files(List[UploadFile]): 文件列表对象
    thread_id(str): 关联的会话ID
    """

    # 1. 确保上传目录存在
    target_dir = upload_dir/f"session_{thread_id}"
    target_dir.mkdir(exist_ok=True, parents=True)

    # 2. 保存并写入文件
    saved_files = []
    for file in files:
        file_path = target_dir / file.filename
        # 使用二进制模式写入
        # shutil.copyfileobj 高效复制文件流，避免一次性加载大文件到内存
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        saved_files.append(file.filename)

    # 3.返回成功保存的文件列表
    return {"status": "uploaded", "files": saved_files}


# 下载文件接口
@app.get("/api/download")
async def download_file(path:str):
    """
    文件下载接口

    目标：
    1.根据绝对路径下载文件
    2.严格的安全检查，防止越权访问
    :param path: 文件的绝对路径（从last_files接口获取）

    """

    # 1.安全检查，并进行路径解析
    try:
        abs_path = Path(path).resolve()
        output_path = output_dir.resolve()
        if not abs_path.is_relative_to(output_path):
            return {"error":"拒绝访问：只能下载输出目录下的文件"}

    except Exception:
        return {"error":"无效的路径参数"}

    # 2.存在性检查
    if not abs_path.exists():
        return {"error":"文件不存在"}

    # 3.返回文件流
    return FileResponse(abs_path, filename=abs_path.name)


# 查询所有文件列表接口
@app.get("/api/files")
async def list_files(path: str):
    """
    文件列表查询接口

    目标：
    1. 列出指定目录下的所有生成文件
    2. 提供文件元数据
    3. 严格的安全检查
    :param path: 目标目录的绝对路径
    """

    try:
        # 1. 获取绝对路径对象
        abs_path = Path(path).resolve()
        output_path = output_dir.resolve()

        # 2.安全检查
        if not abs_path.is_relative_to(output_path):
            logger.error(f"[ERROR]拒绝访问:{abs_path} 不在{output_path}目录下")
            return {"error":"拒绝访问：只能访问输出目录下的文件"}

    except Exception as e:
        logger.error(f"路径解析失败:{e}")
        return {"error":f"路径无效{e}"}

    # 3.检查目录是否存在
    if not abs_path.exists():
        return {"error":"目录不存在"}

    files = []
    try:
        # 4.遍历查找所有文件
        for file_path in abs_path.rglob("*"):
            if file_path.is_file():
                # 计算相对路径，生成下载 URL
                stat = file_path.stat()
                files.append({
                    "name": file_path.name,
                    "type": "file",
                    "path": str(file_path),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime
                })
    except Exception as e:
        logger.error(f"遍历文件失败{e}")
        return {"error":str(e)}

    # 按修改时间倒序排序
    files.sort(key=lambda x: x.get("mtime",0), reverse=True)
    return {"files": files}


# WebSocket实时通讯
@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """
    WebSocket实时通讯核心接口

    目标：
    1.建立长连接，实现服务器和前端的双向通信
    2.绑定'thread_id'，实现会话级消息隔离
    3.维持心跳，防止连接超时

    执行步骤：
    1.握手：接受 Websocket连接请求
    2.注册：将连接实例绑定到'monitor.manager'，关联到'thread_id'
    3.循环：进入消息监听循环，处理前端发送的心跳或者指令
    4.异常：捕获：捕获异常断开连接，清理资源
    :param websocket: websocket连接实例
    :param thread_id: 会话id的唯一标识
    """
    # 1.建立连接并绑定到管理器
    await manager.connect(websocket, thread_id)
    try:
        # 2.保持活跃连接
        while True:
            # 3.监听接收前端消息(通常是ping)
            data = await websocket.receive_text()

            # 4.回复pong消息
            await websocket.send_json({
                "type":"pong",
                "message":f"服务端已收到：{data}"
            })

    except WebSocketDisconnect:
        # 5.清理客户端连接
        manager.disconnect(websocket,thread_id)
        logger.info(f"[WebSocket]客户端已断开：{thread_id}")

    except Exception as e:
        # 6.异常处理
        logger.error(f"[WebSocket]连接异常：{e}")
        manager.disconnect(websocket,thread_id)

# if __name__ == "__main__":
#     uvicorn.run("app.server:app", host="0.0.0.0", port=8000,reload=True)
# 项目根目录下使用 uv run uvicorn api.server:app --reload --host 0.0.0.0 --port启动
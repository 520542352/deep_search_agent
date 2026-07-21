from _contextvars import ContextVar
from typing import Optional
# 存储当前会话对应的文件夹
_session_dir_ctx: ContextVar[Optional[str]] = ContextVar("session_dir",default=None)

# 存储当前会话对应的websocket
_thread_id_ctx: ContextVar[Optional[str]] = ContextVar("thread_id",default=None)

def set_session_context(path: str):
    return _session_dir_ctx.set(path)

def get_session_context() -> Optional[str]:
    return _session_dir_ctx.get()

def set_thread_context(thread_id: str):
    return _thread_id_ctx.set(thread_id)

def get_thread_context() -> Optional[str]:
    return _thread_id_ctx.get()

def reset_session_context(session_token, thread_token=None):
    _session_dir_ctx.reset(session_token)
    if thread_token:
        _thread_id_ctx.reset(thread_token)

if __name__ == "__main__":
    import asyncio
    import random
    # 测试用例

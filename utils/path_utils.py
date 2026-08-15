import os
import re
from pathlib import Path
from typing import Optional

from loguru import logger

_SAVE_THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

def validate_thread_id(thread_id: str) -> str:
    """会话 ID会参与目录名拼接，因此只允许安全的ASCII字符"""
    if not thread_id or not _SAVE_THREAD_ID.fullmatch(thread_id):
        logger.warning(f"thread_id {thread_id} is not valid")
        raise ValueError("thread_id 只能包含数字、字符串、下划线和连字符，长度1-128")
    return thread_id


def _ensure_within_session(candidate:Path, session_path:Path) ->Path:
    """解析路径并强制其位于当前会话目录中"""
    resolve_candidate = candidate.resolve()
    try:
        resolve_candidate.relative_to(session_path)
    except ValueError as exc:
        raise ValueError(f"拒绝访问当前会话目录之外的路径：{resolve_candidate}") from exc
    return resolve_candidate


def resolve_path(filename: str, session_dir: Optional[str] = None) -> str:
    if not filename or not filename.strip():
        raise ValueError("文件路径不能为空")

    path_str = filename.strip().replace("\\", "/")  # 统一处理字符串匹配

    # 虚拟路径清洗
    virtual_prefixes = ["/workspace", "/mnt/data", "/home/user"]
    for prefix in virtual_prefixes:
        if path_str.startswith(prefix):
            # 去掉前缀
            cleaned = path_str[len(prefix):].lstrip("/")
            path_str = cleaned
            break

    if not session_dir:
        raise ValueError("未绑定回话目录，拒绝读写文件")


    session_path = Path(session_dir).resolve()
    session_name = session_path.name
    path = Path(path_str)

    # 模型有时会重复传入 output/session_xxx/file.md。从 session 目录名
    # 之后截取，避免生成 session_xxx/output/session_xxx/file.md。
    if not path.is_absolute() and session_name in path.parts:
        session_index = path.parts.index(session_name)
        relative_parts = path.parts[session_index +1:]
        candidate = session_path.joinpath(*relative_parts)
    elif path.is_absolute():
        candidate = path
    elif os.name == "nt" and path_str.startswith("/"):
        candidate = session_path / path_str.lstrip("/")
    else:
        candidate = session_path / path

    return str(_ensure_within_session(candidate, session_path))

from loguru import logger

from agent.sub_agents.knowledge_base_agent import knowledge_base_agent
from agent.sub_agents.database_query_agent import database_query_agent
from agent.sub_agents.network_search_agent import network_search_agent

# main_agent tool导入
from tools.markdown_tool import generate_markdown
from tools.pdf_tool import convert_md_to_pdf
from tools.upload_file_read_tool import read_file

from deepagents import create_deep_agent

from agent.llm import model
from agent.prompts import main_agent_config

from api.monitor import monitor

import shutil
from pathlib import Path

from api.context import (
    get_run_context,
    reset_session_context,
    set_session_context,
    set_thread_context,
)

from langchain_core.messages import AIMessage

from utils.path_utils import validate_thread_id


# 1.搭建多智能体结构
subagents_list = [
    knowledge_base_agent,
    database_query_agent,
    network_search_agent
]
main_agent_tools = [generate_markdown, convert_md_to_pdf, read_file]


def build_main_agent(
    checkpointer=None,
    *,
    chat_model=model,
    subagents=None,
    tools=None,
):
    """Build the main graph with dependencies owned by the application runtime."""
    return create_deep_agent(
        model=chat_model,
        subagents=subagents_list if subagents is None else subagents,
        tools=main_agent_tools if tools is None else tools,
        system_prompt=main_agent_config["system_prompt"],
        checkpointer=checkpointer,
    )

project_root_path = Path(__file__).parents[1].resolve()
# 3.创建辅助函数

async def run_deep_agent(
    task_query: str,
    thread_id: str = None,
    *,
    agent,
    checkpoint_id: str | None = None,
):
    session_token = None
    thread_token = None
    final_result = ""
    try:
        # 1.[准备环境] 创建目录、处理上传文件
        session_dir_str, relative_session_dir, upload_info = _prepare_session_environment(thread_id)

        # 2. [上下文绑定] 初始化ContextVars（用于隔离并发请求）
        thread_token = set_thread_context(thread_id)
        session_token = set_session_context(session_dir_str)
        monitor.report_session_dir(session_dir_str)

        # 3. [运行时配置] Langchain Config
        config = _agent_config(thread_id, checkpoint_id=checkpoint_id)

        # 4. [提示词构建] 动态注入环境约束
        path_instruction = f"""
        [工作目录环境指令]
        工作目录：{relative_session_dir}
        {upload_info}

        规则：
        1. 新生成文件必须保存到工作目录：'{relative_session_dir}/filename'
        2. 使用相对路径，禁止使用绝对路径
        3. 若存在上传文件，请先分析内容
        """

        # 5. [流式执行] 启动Agent循环
        async for chunk in agent.astream(
                {"messages":[{"role":"user", "content":task_query + path_instruction}]},
                config=config
        ):
            chunk_result = _process_stream_chunk(chunk)
            if chunk_result is not None:
                final_result = chunk_result
        return final_result

    except Exception as e:
        # 7. 异常处理
        logger.error(f"Error:{e}")
        monitor._emit("error", f"Exception failed: {str(e)}")
        raise

    finally:
        # 7. [资源处理] 必须重置ContextVars，防止线程池复用导致上下文污染
        if session_token is not None:
            reset_session_context(session_token, thread_token)


async def resume_deep_agent(
    thread_id: str,
    *,
    agent,
    checkpoint_id: str | None = None,
):
    session_token = None
    thread_token = None
    final_result = ""
    config = _agent_config(thread_id, checkpoint_id=checkpoint_id)
    try:
        session_dir_str = _prepare_resume_environment(thread_id)
        thread_token = set_thread_context(thread_id)
        session_token = set_session_context(session_dir_str)
        monitor.report_session_dir(session_dir_str)

        async for chunk in agent.astream(None, config=config):
            chunk_result = _process_stream_chunk(chunk)
            if chunk_result is not None:
                final_result = chunk_result

        if not final_result:
            snapshot = await agent.aget_state(config)
            if snapshot.next:
                raise RuntimeError("checkpoint resume did not complete")
            messages = snapshot.values.get("messages", [])
            for message in reversed(messages):
                if isinstance(message, AIMessage) and not message.tool_calls:
                    final_result = str(message.content)
                    break
            if final_result:
                monitor.report_task_result(final_result)
        return final_result
    except Exception as e:
        logger.error(f"Error:{e}")
        monitor._emit("error", f"Exception failed: {str(e)}")
        raise
    finally:
        if session_token is not None:
            reset_session_context(session_token, thread_token)


def _prepare_resume_environment(thread_id: str) -> str:
    """Restore only the output workspace; do not recopy uploads on resume."""
    thread_id = validate_thread_id(thread_id)
    session_dir = project_root_path / "output" / f"session_{thread_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return str(session_dir).replace("\\", "/")


def _agent_config(thread_id: str, *, checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": thread_id}
    run_id = get_run_context()
    if run_id is not None:
        configurable["persistence_run_id"] = run_id
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


# 辅助函数：_prepare_session_environment用于初始化会话的运行环境（会话文件夹、相对路径、上传文件信息）
def _prepare_session_environment(thread_id: str):
    """
    初始化会话运行环境
    目标：
    1. 创建独立的物理工作空间
    2. 处理用户上传的文件
    3. 生成供 Agent 和前端使用的路径上下文
    :param thread_id: 会话ID
    """
    # 补充：校验thread_id是否合法

    thread_id = validate_thread_id(thread_id)


    #1. 创建会话输出的绝对路径对应的文件夹
    session_dir = project_root_path /"output"/f"session_{thread_id}"
    session_dir.mkdir(parents=True, exist_ok=True) #如果上级目录不存在，创建出来；如果文件夹已存在，不报错，跳过创建

    #2. 将路径转换为 POSIX 风格（防止大模型因反斜杠产生幻觉）
    session_dir_str = str(session_dir).replace("\\", "/")

    #3. 获取相对路径
    relative_session_dir = str(session_dir.relative_to(project_root_path)).replace("\\", "/")

    #4. 检查并且处理上传文件
    upload_dir = project_root_path /"upload"/f"session_{thread_id}"
    upload_info = ""

    if upload_dir.exists():
        files = [f.name for f in upload_dir.iterdir() if f.is_file()]
        if files:
            for f in files:
                # 核心动作：将文件从临时工作区复制到正式工作区
                shutil.copy2(upload_dir/f, session_dir/f)

            #5. 生成文件列表的提示词
            upload_info = (f"\n [已上传文件] 已加载到工作目录:\n" +
                           "\n".join([f"    -{f}" for f in files]) +
                           "\n   请优先使用工具(read_file)读取并参考这些文件。")

    return session_dir_str, relative_session_dir, upload_info

# 辅助函数：_process_stream_chunk 用于处理Langraph输出的增量状态
def _process_stream_chunk(chunk):
    final_result = None
    # 1.解析每个节点的输出
    for node_name, state in chunk.items():
        if not state or "messages" not in state: continue
        # 2.获取最新一条消息
        messages = state["messages"]
        if isinstance(messages, list) and messages:
            last_msg = messages[-1]
            # 3.[分支]处理 AI 消息（AIMessage）
            if isinstance(last_msg, AIMessage):
                # Case1: Agent决定调用工具
                if last_msg.tool_calls:
                    for tool in last_msg.tool_calls:
                        # 特殊处理：如果是’task‘工具，说明正在委派给子agent
                        if tool['name'] == 'task':
                            monitor.report_assistant(
                                tool['args'].get('subagent_type', 'Agent'),
                                {"desc":tool['args'].get('description')}
                            )
                # Case2:Agent生成最终回复结果
                elif last_msg.content:
                    monitor.report_task_result(last_msg.content)
                    final_result = str(last_msg.content)
    return final_result



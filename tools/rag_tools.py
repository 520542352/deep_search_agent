import os
from typing import Tuple, Optional, Annotated

from dotenv import load_dotenv
from langchain_core.tools import tool
from ragflow_sdk import RAGFlow

from api.monitor import monitor


# 获取rag环境配置
def _load_ragflow_env() ->Tuple[Optional[str], Optional[str]]:  # Optional 表示既可以是 str，也可以是 None
   load_dotenv()
   api_key = os.getenv("RAGFLOW_API_KEY")
   base_url = os.getenv("RAGFLOW_API_URL")
   return api_key, base_url

# get_assistant_list 获取聊天助手和知识库信息
@tool
def get_assistant_list(
        dummy_arg: Annotated[str, "不需要输入参数，直接调用即可"] ="",
) ->str:
    """
    【工具功能】获取 RAGFlow 中所有聊天助手信息
    适用场景：Agent 需要确认当前有哪些可用助手，及每个助手绑定的知识库范围时调用
    返回：结构化字符串（助手名称+功能介绍+关联知识库）
    """
    monitor.report_tool("RAGFlow助手查询列表")
    api_key, base_url = _load_ragflow_env()
    # 配置校验
    if not api_key or not base_url:
        return "错误：未成功配置api_key或者base_url"
    result = ""
    try:
        rag = RAGFlow(api_key=api_key, base_url=base_url)
        # 获取所有聊天助手信息
        for assistant in rag.list_chats():
            # 解析知识库名称assistant.datasets
            kb_names = []
            if assistant.datasets and isinstance(assistant.datasets, list):
                for dataset in assistant.datasets:
                    print(dataset)
                    if isinstance(dataset, dict) and "name" in dataset:
                        kb_names.append(dataset["name"])

            # 格式化知识库
            kb_names_str = "、".join(kb_names) if kb_names else "无"
            # 拼接助手信息
            result += f"助手名称:{assistant.name}; 功能介绍:{assistant.description}; 关联知识库:{kb_names_str}\n"
        # 移除末尾换行符
        return result.rstrip("\n") if result else "未找到聊天助手"
    except Exception as e:
        return f"获取聊天助手失败：{str(e)}"


# create_ask_delete 创建提问和删除会话获取rag查询结果
@tool
def create_ask_delete(
        assistant_name: Annotated[str,"必填:目标助手的名称"],
        question: Annotated[str, "必填：要向助手提问的问题"],
) ->str:
    """
    【工具功能】向指定 RAGFlow 助手发起单次提问（临时会话，用完即删）
    适用场景：Agent 需单次查询某个助手，无需保留会话记录时调用
    特点：创建临时会话→流式接收答案→自动删除会话，无数据残留
    """
    # 调用监控，记录问题
    monitor.report_tool(
        "RAGFlow助手提问工具",
        {"助手名称": assistant_name, "查询问题": question}
    )
    # 获取参数
    api_key, base_url = _load_ragflow_env()
    # 处理提问逻辑
    try:
        rag = RAGFlow(api_key=api_key, base_url=base_url)
        assistants = rag.list_chats(name=assistant_name)
        if not assistants:
            return  f"错误：未找到名为「{assistant_name}」的聊天助手"
        # assistants = [{'avatar': '', 'create_date': '', 'create_time': , 'dataset_ids': , 'datasets': [{'avatar'...之类的结构
        # 因此创建一个助手的话取assistants[0]就能拿到该助手的全部信息
        assistant = assistants[0]

        session = None
        try:
            # 创建临时会话
            session = assistant.create_session(name="temp_session_for_single_ask")
            # 流式提问
            response_generator = session.ask(question, stream=True)
            # 收集流式响应
            full_answer = ""
            for part in response_generator:
                if hasattr(part, "content") and part.content:
                    full_answer = part.content

            # 监控，记录返回答案
            monitor.report_tool(
                "RAGFlow助手返回的答案",
                {"助手名称":assistant_name, "问题": question, "答案": full_answer}
            )
            # 自动删除会话
            if session and hasattr(session, "id"):
                assistant.delete_sessions(ids=[session.id])
            return full_answer if full_answer else "未获取到助手的回答"
        except Exception as e:
            return f"提问过程失败{str(e)}"
    except Exception as e:
        return f"RAGFlow 操作失败 {str(e)}"



if __name__ == "__main__":
    print(create_ask_delete("空调安装助手","简单说一下窗式的空调安装步骤有哪些"))
    print(get_assistant_list())
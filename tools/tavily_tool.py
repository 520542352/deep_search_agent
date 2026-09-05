import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient

from api.monitor import monitor
from loguru import logger

load_dotenv()


def _create_tavily_client():
    """Create the SDK client on demand so importing this module stays offline."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return None
    return TavilyClient(api_key=api_key)

# 定义网络搜索工具
@tool
def internet_search(
        query:str,
        max_results:int = 5,
        topic: Literal["general", "news", "finance"] = "general",
        include_raw_content:bool = False
):
    """
     根据问题进行网络查询，当需要获取外部互联网的公开信息、最新新闻或特定主题数据时使用此工具
     核心用途：
         当 AI Agent 需要获取外部互联网的公开信息、时效性数据（如新闻、金融动态）时调用，
         替代传统搜索引擎，返回更适配大模型的结构化结果。
     参数说明：
         query: 搜索的核心问题/关键词，例如 "2026年AI行业政策"
         max_results: 控制返回结果数量，免费版建议不超过5
         topic: 限定搜索内容类型，提升结果相关性
         include_raw_content: 是否返回详细新闻，False简略版本 True详细版本
     返回值：
         dict: Tavily API 返回的结构化结果，包含以下核心字段：
             - query: 原始搜索词
             - results: 搜索结果列表，每个元素包含 url、content（摘要）、raw_content（原始内容，可选）等
         str: 初始化失败时返回错误提示字符串
     异常处理：
         捕获客户端初始化或搜索异常并返回可读错误，避免外部服务故障中断 Agent。
    """
    try:
        tavily_client = _create_tavily_client()
    except Exception as exc:
        logger.error(f"Tavily 客户端初始化失败: {exc}")
        return f"Error: Tavily 客户端初始化失败：{exc}"

    if tavily_client is None:
        logger.error("未配置 TAVILY_API_KEY")
        return "Error: 未配置 TAVILY_API_KEY，无法执行网络搜索。"

    # 调用工具的时候，monitor会向前端推进进度
    # 参数1：调用工具的名称   参数2：调用工具的参数
    monitor.report_tool(tool_name="网络搜索工具", args={"query": query,"topic": topic,
                                                        "max_results": max_results,"include_raw_content":include_raw_content})
    try:
        results = tavily_client.search(query=query,topic=topic,max_results=max_results,
                                       include_raw_content=include_raw_content)
        return results
    except Exception as exc:
        logger.error(f"Tavily 搜索失败: {exc}")
        return f"Error: Tavily 搜索失败：{exc}"

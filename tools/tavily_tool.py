import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient

from api.monitor import monitor
from loguru import logger
load_dotenv()
if TavilyClient:
    tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
else:
    tavily_client = None

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
         捕获搜索过程中的所有异常并重新抛出，确保 Agent 能感知到搜索失败并处理
     """
    if not tavily_client:
        logger.error("未创建正确的客户端")
        return "Error: 'tavily_client' is not installed.'"

    # 调用工具的时候，monitor会向前端推进进度
    # 参数1：调用工具的名称   参数2：调用工具的参数
    monitor.report_tool(tool_name="网络搜索工具", args={"query": query,"topic": topic,
                                                        "max_results": max_results,"include_raw_content":include_raw_content})
    try:
        results = tavily_client.search(query=query,topic=topic,max_results=max_results,
                                       include_raw_content=include_raw_content)
        return results
    except Exception as e:
        raise e

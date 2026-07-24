import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient

from api.monitor import monitor

load_dotenv()
if TavilyClient:
    tavily_client = TavilyClient(api_key=os.getenv("TVILY_API_KEY"))
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
    if not tavily_client:
        return "Error: 'tavily_client' is not installed.'"
    # 调用工具的时候，会向前端推进进度
    # 参数1：调用工具的名称   参数2：调用工具的参数
    monitor.report_tool(tool_name="网络搜索工具", args={"query": query,"topic": topic,
                                                        "max_results": max_results,"include_raw_content":include_raw_content})
    try:
        results = tavily_client.search(query=query,topic=topic,max_results=max_results,
                                       include_raw_content=include_raw_content)
        return results
    except Exception as e:
        raise e

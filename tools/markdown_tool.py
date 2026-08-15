from pathlib import Path
from loguru import logger
from langchain_core.tools import tool

from api.context import get_session_context
from api.monitor import monitor
from utils.path_utils import resolve_path

try:
    from typing import Annotated
except ImportError:
    from typing_extensions import Annotated


@tool
def generate_markdown(
        content: Annotated[str, "要写入Markdown文档的文本内容"],
        filename: Annotated[str, "Markdown文档的文件名(不包含扩展名或包含.md)"],
        path: Annotated[str, "文件保存的绝对路径"] = ""
) :
    """根据提供的文本内容，生成对应的Markdown(.md)文件"""
    # 埋点监控
    monitor.report_tool("Markdown文档生成工具", {"写入的文本内容":content})
    if not filename.endswith('.md'):
        filename = filename + '.md'

    # 获取上下文中的会话目录
    session_dir = get_session_context()
    logger.info(f"generate_markdown里拿到的session_dir：{session_dir}")  # 看这里！

    # 路径清洗与重定向
    if path and path != ".":
        # 使用Path拼接，再转换为字符串传给 resolve_path
        full_input_path = str(Path(path)/filename)
    else:
        full_input_path = filename
    try:

        full_path_str = resolve_path(full_input_path, session_dir)
        file_path = Path(full_path_str)

        # 获取父目录
        parent_dir = file_path.parent
        logger.info(f"[MarkdownTool] Debug: parent_dir={parent_dir}, filename={filename}, full_path={file_path}")

        if not parent_dir.exists():
            parent_dir.mkdir(parents=True,exist_ok=True)
            logger.info(f"[MarkdownTool] Created directory: {parent_dir}")

        # 使用Path直接写入文本
        file_path.write_text(content, encoding="utf-8")
        logger.info(f"[MarkdownTool] successfully wrote to: {file_path}")
        return f"Markdown文件 '{file_path}' 已成功生成并保存。"
    except Exception as e:
        logger.error(f"[MarkdownTool] Error writing file:{e}")
        return f"生成Markdown文档失败：{e}"

# =========================== 测试代码 =======================
if __name__ == "__main__":
    # 不用Mock，直接重新定义这个函数，给session_dir赋值！
    def get_session_context():
        return "./test_session_123"  # 你要的session_dir初始化值，随便改

    test_content = "# 测试文档\n这是给session_dir配置固定值后的测试内容"
    test_filename = "测试文件"  # 无.md后缀，测试自动补全
    test_path = "sub_dir"       # 相对路径

    # 调用生成函数
    print("===== 开始测试（session_dir已配置为：./test_session_123） =====")
    result = generate_markdown.invoke({
        "content": test_content,
        "filename": test_filename,
        "path": test_path
    })

    # 验证结果
    print(f"\n调用结果：{result}")
    if "已成功生成" in result:
        file_path = Path(result.split("'")[1])
        print(f"验证：文件 {file_path} {'存在' if file_path.exists() else '不存在'}")

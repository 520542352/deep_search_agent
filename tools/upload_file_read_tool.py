from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

from api.context import get_session_context
from api.monitor import monitor
from utils.path_utils import resolve_path

# 导入可选依赖，按需加载
try:
    import docx
except ImportError:
    docx = None

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import pandas as pd
except ImportError:
    pd = None

# 创建文件读写工具
@tool
def read_file(
        filename: Annotated[str, "要读取的文件名或路径(支持.md, .docx, .pdf, .xlsx, .xls)"],
        instruction: Annotated[str, "对提取内容的具体指令（例如：‘提取摘要’，‘统计其中的某类数据’）"] = "提取全部内容"
) ->str:
    """
        读取指定文件的内容。支持 Markdown(.md)、Word(.docx)、PDF(.pdf) 和 Excel(.xlsx/.xls)。
        对于 Excel 文件，会自动提供数据统计信息（head 和 describe）。
    """
    # 监控埋点
    monitor.report_tool("文件路径内容读取工具",{"filename":filename,"instruction":instruction})
    # Path 重构文件路径
    session_dir = get_session_context()
    file_path = Path(resolve_path(filename, session_dir)) # 转换为path对象

    # 检查文件是否存在
    if not file_path.exists():
        return f"错误：文件'{filename}' 不存在(解析路径：{file_path})."
    # 获取后缀名
    ext = file_path.suffix.lower()

    try:
        if ext in ['.md', '.txt']:
            return file_path.read_text(encoding="utf-8")

        elif ext == '.docx':
            if docx is None:
                return "错误，未安装 py-docx 库，无法读取docx"
            doc = docx.Document(str(file_path))
            full_text = [para.text for para in doc.paragraphs]
            return '\n'.join(full_text)

        elif ext == '.pdf':
            if pypdf is None:
                return "错误，未安装 pypdf 库，无法读取pdf"
            pdf = pypdf.PdfReader(str(file_path))
            text = "\n".join([page.extract_text() or "" for page in pdf.pages])
            return text

        elif ext in ['.xls', '.xlsx']:
            if pd is None:
                return "错误：未安装pandas库，无法读取Excel文件"
            try:
                df = pd.read_excel(str(file_path))
            except Exception as e:
                return f"读取 Excel 失败{str(e)}"

            result = [
                f"文件：{filename}",
                f"行数{len(df)},列数：{len(df.columns)}",
                f"列名：{','.join(df.columns.astype(str))}",
                "\n[前5行数据预览]:",
                df.head().to_string(index=False),
                "\n[统计描述]:",
                df.describe().to_string()
            ]
            return '\n'.join(result)
        else:
            try:
                return file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return f"错误：不支持的文件格式'{ext}'，且无法作为文本读取"
    except Exception as e:
        return f"读取文件出错{str(e)}"

# =================== 测试入口 =====================
if __name__ == '__main__':
    # 1. 固定 session_dir（仅赋值，不Mock）
    def get_session_context():
        return "./test_session_123"


    # 2. 定义测试文件路径
    md_path = "sub_dir/测试文件.md"
    excel_path = "sub_dir/测试数据.xlsx"

    # 3. 测试调用（先测试MD文件，指令用默认）
    result = read_file.invoke({
        "filename": md_path
    })
    print("===== 读取MD文件结果 =====")
    print(result)

    # 可选：测试Excel文件（取消注释即可）
    # result_excel = read_file_content.invoke({
    #     "filename": excel_path,
    #     "instruction": "统计数据"
    # })
    # print("\n===== 读取Excel文件结果 =====")
    # print(result_excel)

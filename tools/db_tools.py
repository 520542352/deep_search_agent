import os

from typing import Annotated,List

from langchain_core.tools import tool
from loguru import logger
from dotenv import load_dotenv
from mysql.connector import connect, Error
from sqlalchemy import text

from api.monitor import monitor

load_dotenv()
# 加载配置文件
def get_db_config():
    config={
        "host": os.getenv("MYSQL_HOST","localhost"), # (key, default)表示读不到就用default值
        "port": int(os.getenv("MYSQL_PORT","3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET","utf8mb4"),
        "collation": os.getenv("MYSQL_COLLATION","utf8mb4_unicode_ci"),
        "autocommit": True,
        "sql_mode": os.getenv("MYSQL_SQL_MODE","TRADITIONAL")
    }
    # 去除空值项
    config = {k: v for k,v in config.items() if v is not None}
    # 校验核心配置是否存在
    required_keys = ["user", "password", "database"]
    missing_key = [k for k in required_keys if k not in config]
    if missing_key:
        msg = f"Missing required key: {', '.join(missing_key)}"
        logger.error(msg)
        raise ValueError(msg)
    return config

# @tool
def list_sql_tables() -> Annotated[str, "数据库中可用的表明列表，以逗号分隔"]:
    #def list_sql_tables() -> Annotated[str, "数据库中可用的表明列表，以逗号分隔"]:
    monitor.report_tool("数据库表获取工具")
    # 获取数据库配置
    config = get_db_config()
    try:
        if not all([config.get("user"), config.get("password"), config.get("database")]):
            return "错误：数据库配置缺失"
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute("show tables;")
                # DQL结果解析
                tables = cursor.fetchall()
                if not tables:
                    return "数据库无数据"
                table_names = [table[0] for table in tables]
                return f"可用数据表: {', '.join(table_names)}"
    except Exception as e:
        logger.error(f"Failed to read tables: {str(e)}")
        return f"列出数据表失败: {str(e)}"


# @tool
def get_table_data(
        table_name: Annotated[str, "要读的数据表表名"]
) -> Annotated[str, "表前100行数据"]:
    monitor.report_tool("数据库内容浏览工具", {"正在读的表":table_name})

    config = get_db_config()
    try:
       if not all([config.get("user"), config.get("password"), config.get("database")]):
            return "错误：数据库配置缺失"

       # 建立数据库连接并创建游标
       with connect(**config) as conn:
           with conn.cursor() as cursor:
               safe_table_name = table_name.replace("`","").replace(";","").split()[0]
               sql = f"select * from `{safe_table_name}` limit 100"
               cursor.execute(sql)
               # 获取sql结果
               # description -> [(id,...), (name),()...]
               if cursor.description is None:
                   return f"数据表: {table_name} 为空"
               # 获取列表名
               columns = [desc[0] for desc in cursor.description]
               # 获取列内容
               # [(1,张三),(2,李四),(...)]
               rows = cursor.fetchall()
               # (1,张三) ->('1','张三') -> '1','张三'
               result = [",".join(map(str, row)) for row in rows] # map：将row转换为str，拼接起来并以,隔开
               # csv格式：
               #id,name,age\n ->列头
               #1,张三,18\n
               #2,李四,20\n  ->最多100条
               header ="," .join(columns)

               return f"{header}\n" + "\n".join(result)

    except Exception as e:
        logger.error(f"Failed to read table {table_name}: {str(e)}")
        return f"读取数据库{table_name}失败: {str(e)}"

# @tool
def execute_sql_query(
        query: Annotated[str,"要执行的SQL查询语句"]
) -> Annotated[str, "查询成功"]:
    monitor.report_tool("数据库查询工具")
    config = get_db_config()
    try:
        if not all([config.get("user"), config.get("password"), config.get("database")]):
            return "数据库配置信息错误"
        # 建立连接
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                if cursor.description is not None:
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()

                    if not rows:
                        return f"查询执行成功，无数据返回。涉及列名：{','.join(columns)}"

                    # 构造 csv 格式的返回结果
                    result_lines = []
                    result_lines.append(','.join(columns))
                    for row in rows:
                        result_lines.append(','.join(map(str, row)))
                    return '\n'.join(result_lines)

                else:
                    return f"SQL 执行成功，受以下行数影响：{cursor.rowcount}"

    except Error as e:
        logger.error(f"Failed to execute query: {str(e)}")
        return f"执行SQL 失败{str(e)}"




'''
def parser_table(result: str) ->list[str]:
    if not result.startswith("可用数据表:"):
        return []
    tables_part = result.replace("可用数据表:", "").strip()
    return [t.strip() for t in tables_part.split(",")]
'''

#if __name__ == "__main__":
    #print(list_sql_tables())
    # result_table = list_sql_tables()
    # table_names = parser_table(result_table)
    # for table_name in table_names:

    #print(get_table_data("drugs"))
    #print(execute_sql_query("select * from `drugs` dgs join sales_records srd on dgs.drug_id = srd.drug_id;"))
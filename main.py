# test_starstar.py

def fake_create_engine(url, host, port, user, password, database, debug=False):
    print("收到参数：")
    print(f"  url      = {url}")
    print(f"  host     = {host}")
    print(f"  port     = {port}")
    print(f"  user     = {user}")
    print(f"  database = {database}")
    print(f"  debug    = {debug}")


def get_db_config():
    return {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "123",
        "database": "deep_agent",
        "debug": True
    }


if __name__ == "__main__":
    # print("===== 没有 ** 的情况 =====")
    # cfg = get_db_config()
    # print("cfg 是个字典：", cfg)
    #
    # print("\n===== 有 ** 的情况 =====")
    # fake_create_engine("mysql://x", **cfg)
    row = (1, 3.14, None, "abc")

    result = map(str, row)
    print(result)  # <map object at ...>
    print(list(result))  # ['1', '3.14', 'None', 'abc']
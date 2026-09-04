from types import SimpleNamespace

import pytest

from tools import db_tools


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("SELECT 1;", "SELECT 1"),
        ("  SELECT * FROM products  ", "SELECT * FROM products"),
        (
            "WITH recent AS (SELECT * FROM sales) SELECT * FROM recent;",
            "WITH recent AS (SELECT * FROM sales) SELECT * FROM recent",
        ),
        ("SELECT 'DROP TABLE products' AS text", "SELECT 'DROP TABLE products' AS text"),
    ],
)
def test_validate_read_only_query_accepts_safe_sql(query: str, expected: str) -> None:
    assert db_tools.validate_read_only_query(query) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    [
        "",
        "DELETE FROM products",
        "SELECT 1; DROP TABLE products",
        "SELECT * FROM products FOR UPDATE",
        "SELECT LOAD_FILE('/etc/passwd')",
        "WITH removed AS (DELETE FROM products RETURNING *) SELECT * FROM removed",
    ],
)
def test_validate_read_only_query_rejects_unsafe_sql(query: str) -> None:
    with pytest.raises(ValueError):
        db_tools.validate_read_only_query(query)


@pytest.mark.unit
def test_get_db_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_HOST", "db.local")
    monkeypatch.setenv("MYSQL_PORT", "3307")
    monkeypatch.setenv("MYSQL_USER", "reader")
    monkeypatch.setenv("MYSQL_PASSWORD", "secret")
    monkeypatch.setenv("MYSQL_DATABASE", "analytics")

    config = db_tools.get_db_config()

    assert config["host"] == "db.local"
    assert config["port"] == 3307
    assert config["user"] == "reader"
    assert config["database"] == "analytics"


@pytest.mark.unit
def test_get_db_config_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError, match="Missing required key"):
        db_tools.get_db_config()


class _FakeCursor:
    description = [("id",), ("name",)]

    def __init__(self) -> None:
        self.executed = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query: str) -> None:
        self.executed = query

    def fetchmany(self, size: int):
        return [(index, f"row-{index}") for index in range(size)]


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.readonly = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def start_transaction(self, readonly: bool) -> None:
        self.readonly = readonly

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance


@pytest.mark.unit
def test_execute_sql_query_uses_readonly_transaction_and_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    captured_config = SimpleNamespace(value=None)
    monkeypatch.setattr(
        db_tools,
        "get_db_config",
        lambda: {"user": "reader", "password": "secret", "database": "db"},
    )

    def fake_connect(**config):
        captured_config.value = config
        return connection

    monkeypatch.setattr(db_tools, "connect", fake_connect)

    result = db_tools.execute_sql_query.invoke({"query": "SELECT * FROM products"})

    assert connection.readonly is True
    assert captured_config.value["autocommit"] is False
    assert "[结果已截断" in result
    assert len(result.splitlines()) == db_tools.MAX_QUERY_ROWS + 2

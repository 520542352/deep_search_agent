import pytest

from tools import db_tools


pytestmark = pytest.mark.contract
_CONFIG = {"user": "reader", "password": "secret", "database": "analytics"}


class _Cursor:
    def __init__(self, *, rows=(), description=None, error: Exception | None = None) -> None:
        self.rows = list(rows)
        self.description = description
        self.error = error
        self.executed: list[str] = []
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.exited = True

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.error:
            raise self.error

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_instance = cursor
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.exited = True

    def cursor(self):
        return self.cursor_instance


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    cursor: _Cursor,
) -> _Connection:
    connection = _Connection(cursor)
    monkeypatch.setattr(db_tools, "get_db_config", lambda: _CONFIG.copy())
    monkeypatch.setattr(db_tools, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(db_tools.monitor, "report_tool", lambda *args, **kwargs: None)
    return connection


def test_list_tables_formats_names_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(rows=[("users",), ("orders",)])
    connection = _configure(monkeypatch, cursor)

    result = db_tools.list_sql_tables.invoke({})

    assert result == "可用数据表: users, orders"
    assert cursor.executed == ["show tables;"]
    assert cursor.exited is True
    assert connection.exited is True


def test_list_tables_handles_empty_database(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _Cursor(rows=[]))

    assert db_tools.list_sql_tables.invoke({}) == "数据库无数据"


@pytest.mark.parametrize("failure_at", ["config", "connect", "cursor"])
def test_list_tables_converts_failures_to_tool_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    monkeypatch.setattr(db_tools.monitor, "report_tool", lambda *args, **kwargs: None)
    if failure_at == "config":
        monkeypatch.setattr(
            db_tools,
            "get_db_config",
            lambda: (_ for _ in ()).throw(ValueError("missing config")),
        )
    else:
        monkeypatch.setattr(db_tools, "get_db_config", lambda: _CONFIG.copy())
        if failure_at == "connect":
            monkeypatch.setattr(
                db_tools,
                "connect",
                lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("connect failed")),
            )
        else:
            connection = _Connection(_Cursor(error=RuntimeError("cursor failed")))
            monkeypatch.setattr(db_tools, "connect", lambda **_kwargs: connection)

    result = db_tools.list_sql_tables.invoke({})

    assert "列出数据表失败" in result


def test_get_table_data_returns_csv_and_uses_validated_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(
        rows=[(1, "Alice"), (2, "Bob")],
        description=[("id",), ("name",)],
    )
    connection = _configure(monkeypatch, cursor)

    result = db_tools.get_table_data.invoke({"table_name": "users_2026"})

    assert result == "id,name\n1,Alice\n2,Bob"
    assert cursor.executed == ["select * from `users_2026` limit 100"]
    assert cursor.exited is True
    assert connection.exited is True


@pytest.mark.parametrize("table_name", ["", "users; DROP TABLE users", "two tables", "a`b"])
def test_get_table_data_rejects_invalid_identifiers_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    table_name: str,
) -> None:
    monkeypatch.setattr(db_tools.monitor, "report_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        db_tools,
        "connect",
        lambda **_kwargs: pytest.fail("invalid input must be rejected before connecting"),
    )

    result = db_tools.get_table_data.invoke({"table_name": table_name})

    assert "数据表名只能包含" in result


def test_get_table_data_handles_missing_description_and_zero_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, _Cursor(description=None))
    assert db_tools.get_table_data.invoke({"table_name": "users"}) == "数据表: users 为空"

    _configure(monkeypatch, _Cursor(rows=[], description=[("id",), ("name",)]))
    assert db_tools.get_table_data.invoke({"table_name": "users"}) == "id,name\n"


def test_get_table_data_releases_resources_on_cursor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(error=RuntimeError("read failed"))
    connection = _configure(monkeypatch, cursor)

    result = db_tools.get_table_data.invoke({"table_name": "users"})

    assert "read failed" in result
    assert cursor.exited is True
    assert connection.exited is True


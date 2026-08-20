from __future__ import annotations

import json
import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

from tools.remote_query import RemoteQueryError, RemoteQueryTool, run_remote_query


ROOT = Path(__file__).resolve().parents[1]


def test_tool_module_loads_when_a_host_does_not_register_it_in_sys_modules():
    """Dify executes tool sources this way during plugin discovery."""

    source = ROOT / "tools" / "remote_query.py"
    module_name = "_dify_unregistered_remote_query"
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(module_name, None)

    spec.loader.exec_module(module)

    assert callable(module.run_remote_query)


class FakeCursor:
    def __init__(self, rows=None, columns=("id", "name")):
        self.rows = rows if rows is not None else [(1, "one")]
        self.description = [(name,) for name in columns]
        self.calls = []
        self.closed = False

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def fetchmany(self, size):
        return self.rows[:size]

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows=None, columns=("id", "name")):
        self.cursor_obj = FakeCursor(rows, columns)
        self.rollback_called = False
        self.close_called = False

    def cursor(self):
        return self.cursor_obj

    def rollback(self):
        self.rollback_called = True

    def close(self):
        self.close_called = True


def install_fake_driver(monkeypatch, database_type="postgresql", rows=None, columns=("id", "name")):
    connections = []

    def connect(**kwargs):
        connection = FakeConnection(rows, columns)
        connection.connect_kwargs = kwargs
        connections.append(connection)
        return connection

    if database_type == "postgresql":
        package = types.ModuleType("pg8000")
        dbapi = types.ModuleType("pg8000.dbapi")
        dbapi.connect = connect
        package.dbapi = dbapi
        monkeypatch.setitem(sys.modules, "pg8000", package)
        monkeypatch.setitem(sys.modules, "pg8000.dbapi", dbapi)
    else:
        module = types.ModuleType("pymysql")
        module.connect = connect
        monkeypatch.setitem(sys.modules, "pymysql", module)
    return connections


def base_params(sql="SELECT id, name FROM products WHERE state = :state", **extra):
    result = {
        "database_type": "postgresql",
        "host": "db.example",
        "port": 5432,
        "database": "products",
        "username": "readonly",
        "password": "dont-echo-me",
        "ssl_mode": "verify-full",
        "query_mode": "raw_sql",
        "sql": sql,
        "parameters_json": json.dumps({"state": "active"}),
    }
    result.update(extra)
    return result


def test_provider_registers_only_remote_query_and_permissions_are_minimal():
    provider = yaml.safe_load((ROOT / "provider" / "tcpr.yaml").read_text(encoding="utf-8"))
    assert provider["tools"] == ["tools/remote_query.yaml"]
    manifest = yaml.safe_load((ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.0.9"
    assert "node" not in manifest["resource"]["permission"]
    assert "storage" not in manifest["resource"]["permission"]


def test_remote_yaml_keeps_connection_fields_out_of_llm():
    document = yaml.safe_load((ROOT / "tools" / "remote_query.yaml").read_text(encoding="utf-8"))
    parameters = {item["name"]: item for item in document["parameters"]}
    for name in ("query_mode", "database_type", "host", "port", "database", "username", "password", "ssl_mode", "table", "tcpr_schema_json"):
        assert parameters[name]["form"] == "form"
    assert parameters["password"]["type"] == "secret-input"
    assert parameters["sql"]["form"] == "llm"
    assert parameters["tcpr_query_json"]["form"] == "llm"
    assert parameters["parameters_json"]["form"] == "llm"


def test_postgres_query_uses_named_parameters_server_read_only_and_rolls_back(monkeypatch):
    connections = install_fake_driver(monkeypatch)
    result = run_remote_query(base_params())
    connection = connections[0]
    assert result == {
        "status": "OK",
        "database_type": "postgresql",
        "query_mode": "raw_sql",
        "rows": [{"id": 1, "name": "one"}],
        "row_count": 1,
        "columns": ["id", "name"],
        "truncated": False,
    }
    assert connection.cursor_obj.calls[0] == ("BEGIN READ ONLY", None)
    assert "LIMIT 101" in connection.cursor_obj.calls[1][0]
    assert connection.cursor_obj.calls[1][1] == ["active"]
    assert connection.rollback_called and connection.close_called
    assert connection.connect_kwargs["timeout"] == 10
    assert connection.connect_kwargs["startup_params"] == {
        "default_transaction_read_only": "on",
        "statement_timeout": "30000",
    }


def test_mysql_query_uses_server_read_only_transaction(monkeypatch):
    connections = install_fake_driver(monkeypatch, "mysql")
    result = run_remote_query(base_params(
        "SELECT id FROM products WHERE state = :state",
        database_type="mysql", port=3306,
    ))
    assert result["status"] == "OK"
    assert connections[0].cursor_obj.calls[0] == ("START TRANSACTION READ ONLY", None)
    assert connections[0].connect_kwargs["connect_timeout"] == 10


@pytest.mark.parametrize("sql", [
    "UPDATE products SET state='x'",
    "SELECT 1; DELETE FROM products",
    "WITH changed AS (UPDATE products SET state='x' RETURNING id) SELECT * FROM changed",
    "SELECT * FROM products FOR UPDATE",
    "SELECT * INTO OUTFILE '/tmp/x' FROM products",
    "SELECT /*!40101 SET @x=1*/ 1",
])
def test_unsafe_sql_is_rejected_before_connection(monkeypatch, sql):
    connections = install_fake_driver(monkeypatch)
    with pytest.raises(RemoteQueryError) as error:
        run_remote_query(base_params(sql, parameters_json="{}"))
    assert error.value.code == "SQL_NOT_READ_ONLY"
    assert connections == []


def test_literals_comments_and_quoted_identifiers_cannot_trigger_policy(monkeypatch):
    connections = install_fake_driver(monkeypatch)
    result = run_remote_query(base_params(
        "SELECT ':not_a_param', \"UPDATE\" FROM products -- DELETE\nWHERE state = :state",
    ))
    assert result["status"] == "OK"
    assert "SELECT ':not_a_param'" in connections[0].cursor_obj.calls[-1][0]


def test_parameter_object_is_independent_and_exact():
    with pytest.raises(RemoteQueryError, match="missing") as missing:
        run_remote_query(base_params(parameters_json="{}"))
    assert missing.value.code == "PARAMETERS_INVALID"
    with pytest.raises(RemoteQueryError, match="unused") as extra:
        run_remote_query(base_params(parameters_json=json.dumps({"state": "active", "other": 1})))
    assert extra.value.code == "PARAMETERS_INVALID"


def test_duplicate_result_columns_are_rejected(monkeypatch):
    install_fake_driver(monkeypatch, columns=("id", "id"))
    with pytest.raises(RemoteQueryError) as error:
        run_remote_query(base_params("SELECT id, id FROM products", parameters_json="{}"))
    assert error.value.code == "DUPLICATE_COLUMN"


def test_result_limit_serialization_and_sensitive_redaction(monkeypatch):
    rows = [(index, "secret-value") for index in range(101)]
    install_fake_driver(monkeypatch, rows=rows)
    result = run_remote_query(base_params("SELECT id, password FROM products", parameters_json="{}"))
    assert result["row_count"] == 100
    assert result["truncated"] is True

    # Mapping rows are redacted by key name during serialization.
    install_fake_driver(monkeypatch, rows=[{"id": 1, "password": "secret"}])
    mapped = run_remote_query(base_params("SELECT id, password FROM products", parameters_json="{}"))
    assert mapped["rows"] == [{"id": 1, "password": "[REDACTED]"}]


def test_tool_returns_stable_redacted_errors_without_credentials(monkeypatch):
    if hasattr(RemoteQueryTool, "from_credentials"):
        tool = RemoteQueryTool.from_credentials({})
    else:
        # The SDK compatibility layer deliberately has no fake from_credentials
        # method when dify_plugin is absent (for example, system Python 3.11).
        tool = RemoteQueryTool()
    messages = list(tool.invoke({"sql": "DROP TABLE products", "password": "super-secret"})) if hasattr(tool, "invoke") else list(tool._invoke({"sql": "DROP TABLE products", "password": "super-secret"}))
    message = messages[-1]
    payload = message.message.json_object if hasattr(message, "message") else message.value
    assert payload["status"] == "ERROR"
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert "super-secret" not in json.dumps(payload)


def _tcpr_params(**extra):
    result = {
        "database_type": "postgresql",
        "host": "db.example",
        "port": 5432,
        "database": "products",
        "username": "readonly",
        "password": "dont-echo-me",
        "ssl_mode": "verify-full",
        "table": "商品",
        "tcpr_schema_json": json.dumps({
            "primary_key": "编号",
            "fields": {
                "编号": {"column": "编号", "kind": "numeric"},
                "价格": {"column": "价格", "kind": "numeric"},
                "特性": {"column": "特性", "kind": "multi"},
            },
        }, ensure_ascii=False),
        "tcpr_query_json": json.dumps({
            "hard": [{"attr": "价格", "op": "GE", "value": 100}],
            "soft": [{"constraint": {"attr": "特性", "op": "CONTAINS", "value": "wifi6"}, "weight": 2}],
            "select": ["编号", "价格"],
            "top_k": 5,
        }, ensure_ascii=False),
    }
    result.update(extra)
    return result


def test_tcpr_compiles_parameterized_hard_soft_and_unicode_identifiers(monkeypatch):
    connections = install_fake_driver(monkeypatch)
    result = run_remote_query(_tcpr_params())
    assert result["status"] == "OK" and result["query_mode"] == "tcpr"
    sql, args = connections[0].cursor_obj.calls[-1]
    assert '"商品"' in sql and '"价格"' in sql and '"编号"' in sql
    assert "SUM(CASE" in sql and "ORDER BY" in sql and "LIMIT %s" in sql
    assert args[-1] == 6
    assert "wifi6" in json.dumps(args, ensure_ascii=False)


def test_tcpr_default_never_falls_back_to_sql(monkeypatch):
    connections = install_fake_driver(monkeypatch)
    with pytest.raises(RemoteQueryError) as error:
        run_remote_query({**base_params(), "query_mode": "tcpr", "table": "products", "sql": "SELECT 1"})
    assert error.value.code == "INVALID_INPUT"
    assert connections == []


def test_tcpr_unsatisfiable_hard_constraints_do_not_connect(monkeypatch):
    connections = install_fake_driver(monkeypatch)
    params = _tcpr_params(tcpr_query_json=json.dumps({
        "hard": [{"attr": "价格", "op": "EQ", "value": 1}, {"attr": "价格", "op": "EQ", "value": 2}],
        "select": ["编号"],
    }, ensure_ascii=False))
    result = run_remote_query(params)
    assert result["status"] == "OK" and result["rows"] == []
    assert connections == []


def test_tcpr_mysql_uses_json_contains(monkeypatch):
    connections = install_fake_driver(monkeypatch, "mysql")
    result = run_remote_query(_tcpr_params(database_type="mysql", port=3306))
    assert result["status"] == "OK"
    assert "JSON_CONTAINS" in connections[0].cursor_obj.calls[-1][0]

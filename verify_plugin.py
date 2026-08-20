"""Offline runtime and packaging checks for the remote-query plugin."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
EXPECTED_TOOLS = {"remote_query"}
FORM_FIELDS = {"query_mode", "database_type", "host", "port", "database", "username", "password", "ssl_mode", "table", "tcpr_schema_json"}


class _Cursor:
    description = [("id",), ("secret",)]

    def __init__(self):
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))

    def fetchmany(self, size):
        return [{"id": 1, "secret": "do-not-return"}]

    def close(self):
        pass


class _Connection:
    def __init__(self):
        self.cursor_obj = _Cursor()
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _install_fake_postgres():
    connections: list[_Connection] = []

    def connect(**kwargs):
        connection = _Connection()
        connections.append(connection)
        return connection

    package = types.ModuleType("pg8000")
    dbapi = types.ModuleType("pg8000.dbapi")
    dbapi.connect = connect
    package.dbapi = dbapi
    sys.modules["pg8000"] = package
    sys.modules["pg8000.dbapi"] = dbapi
    return connections


def verify_runtime_contracts() -> None:
    provider_doc = yaml.safe_load((ROOT / "provider" / "tcpr.yaml").read_text(encoding="utf-8"))
    tool_paths = provider_doc["tools"]
    assert tool_paths == ["tools/remote_query.yaml"]
    assert not any("build_" in path or "search" in path or "structure" in path for path in tool_paths)

    manifest_doc = yaml.safe_load((ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest_doc["version"] == "0.0.8"
    assert manifest_doc["meta"]["version"] == "0.0.8"
    assert set(manifest_doc["tags"]) == {"business", "utilities"}
    assert manifest_doc["privacy"] == "./PRIVACY.md"
    permissions = manifest_doc["resource"]["permission"]
    assert permissions["tool"]["enabled"] is True
    assert "node" not in permissions and "storage" not in permissions

    document = yaml.safe_load((ROOT / "tools" / "remote_query.yaml").read_text(encoding="utf-8"))
    parameters = {item["name"]: item for item in document["parameters"]}
    assert set(parameters) == FORM_FIELDS | {"sql", "parameters_json", "tcpr_query_json"}
    assert {name for name, item in parameters.items() if item["form"] == "form"} == FORM_FIELDS
    assert parameters["password"]["type"] == "secret-input"
    assert parameters["sql"]["form"] == "llm"
    assert parameters["tcpr_schema_json"]["form"] == "form"
    assert parameters["tcpr_query_json"]["form"] == "llm"
    assert parameters["query_mode"]["default"] == "tcpr"
    assert parameters["parameters_json"]["form"] == "llm"
    assert {"rows", "row_count", "columns", "truncated", "error"} <= set(document["output_schema"]["properties"])

    connections = _install_fake_postgres()
    from tools.remote_query import run_remote_query

    payload = run_remote_query({
        "database_type": "postgresql", "host": "db.example", "port": 5432,
        "database": "products", "username": "readonly", "password": "secret",
        "ssl_mode": "verify-full", "query_mode": "raw_sql",
        "sql": "SELECT id, secret FROM products WHERE state = :state;",
        "parameters_json": json.dumps({"state": "active"}),
    })
    assert payload["status"] == "OK" and payload["row_count"] == 1
    assert payload["database_type"] == "postgresql"
    assert payload["rows"][0]["secret"] == "[REDACTED]"
    assert connections[0].rolled_back and connections[0].closed
    assert payload["query_mode"] == "raw_sql"
    executed_sql, executed_args = connections[0].cursor_obj.executed[-1]
    assert "LIMIT 101" in executed_sql and executed_args == ["active"]

    for sql in (
        "UPDATE products SET state='x'",
        "SELECT 1; DELETE FROM products",
        "WITH changed AS (UPDATE products SET state='x' RETURNING id) SELECT * FROM changed",
        "SELECT * FROM products FOR UPDATE",
        "SELECT * INTO OUTFILE '/tmp/x' FROM products",
    ):
        try:
            run_remote_query({
                "database_type": "postgresql", "host": "db", "database": "d",
                "username": "u", "password": "p", "query_mode": "raw_sql", "sql": sql,
            })
        except Exception as exc:
            assert getattr(exc, "code", "") == "SQL_NOT_READ_ONLY"
        else:
            raise AssertionError(f"unsafe SQL was accepted: {sql}")


def verify_dify_registration() -> None:
    from dify_plugin import DifyPluginEnv
    from dify_plugin.core.plugin_registration import PluginRegistration

    import os
    previous = Path.cwd()
    try:
        os.chdir(ROOT)
        registration = PluginRegistration(DifyPluginEnv())
    finally:
        os.chdir(previous)
    assert registration.configuration.version == "0.0.8"
    assert sorted(registration.tools_mapping["tcpr"][2]) == ["remote_query"]


def main() -> None:
    verify_runtime_contracts()
    verify_dify_registration()
    print("PLUGIN_RUNTIME_CONTRACTS_OK")


if __name__ == "__main__":
    main()

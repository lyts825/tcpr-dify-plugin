"""Offline runtime and packaging checks for the standalone Dify plugin."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tcpr_core.shared_core import CoreService, InMemoryKV


ROOT = Path(__file__).resolve().parent
EXPECTED_TOOLS = {"search", "structure_query", "build_index", "build_database"}
INDEX = {
    "primary_key": "id",
    "attributes": {
        "id": {"kind": "string"},
        "color": {"kind": "enum"},
        "count": {"kind": "numeric", "units": {"": 1}},
    },
}


def verify_runtime_contracts() -> None:
    """Verify the standalone plugin runtime and the manual-index smoke path."""
    provider = (ROOT / "provider" / "tcpr.yaml").read_text(encoding="utf-8")
    for name in EXPECTED_TOOLS:
        assert f"tools/{name}.yaml" in provider, f"missing provider tool: {name}"
    assert provider.count("tools/") == 4, "provider must expose exactly four tools"
    assert not any(
        old in provider for old in ("get_schema", "import_products", "rebuild_index", "tcpr_search")
    )

    manifest = (ROOT / "manifest.yaml").read_text(encoding="utf-8")
    assert "version: 0.0.4" in manifest and "version: '3.12'" in manifest
    manifest_doc = yaml.safe_load(manifest)
    permissions = manifest_doc["resource"]["permission"]
    assert permissions["node"]["enabled"] is True
    assert permissions["tool"]["enabled"] is True
    assert permissions["storage"]["enabled"] is True
    assert permissions["app"]["enabled"] is False and permissions["endpoint"]["enabled"] is False
    assert (ROOT / "tcpr_core" / "bundled" / "tcpr" / "core_api.py").is_file()
    shared = (ROOT / "tcpr_core" / "shared_core.py").read_text(encoding="utf-8")
    assert "bundled.tcpr" in shared
    assert "sync_plugin_core" not in shared and "src/tcpr" not in shared

    rows = [
        {"id": "a", "color": "red", "count": "2"},
        {"id": "b", "color": "blue", "count": "5"},
    ]
    source = {"data": json.dumps(rows).encode(), "filename": "records.json"}
    service = CoreService(InMemoryKV())
    index_id = service.build_index(json.dumps(INDEX, ensure_ascii=False))
    index_payload = service.store.load("index", index_id)
    assert index_payload["primary_key"] == "id"
    assert "postings" not in index_payload
    database_id = service.build_database(source, index_id)
    result = service.search(
        json.dumps({"hard": [{"attr": "color", "op": "EQ", "value": "red"}]}),
        index_id,
        database_id,
    )
    assert result["status"] == "OK", result
    assert [row["id"] for row in result["results"]] == ["a"], result
    structured = service.structure_query("color=red 且 count>=2", index_id)
    assert structured["status"] == "OK"
    assert json.loads(structured["query_json"])["hard"][1]["op"] == "GE"
    dsl = service.structure_index("primary_key: id\nattributes:\n id: string\n color: enum\n count: numeric units=元:1")
    assert dsl["status"] == "OK"


def verify_dify_registration() -> None:
    """Load the real SDK registration and validate workflow-facing schemas."""

    from dify_plugin import DifyPluginEnv
    from dify_plugin.core.plugin_registration import PluginRegistration

    previous = Path.cwd()
    try:
        import os
        os.chdir(ROOT)
        registration = PluginRegistration(DifyPluginEnv())
    finally:
        os.chdir(previous)
    assert registration.configuration.version == "0.0.4"
    assert sorted(registration.tools_mapping["tcpr"][2]) == sorted(EXPECTED_TOOLS)

    expected_outputs = {
        "search": {"count": "integer", "results": "array", "debug": "object", "error": "object"},
        "structure_query": {"query": "object", "error": "object"},
        "build_index": {"error": "object"},
        "build_database": {"error": "object"},
    }
    for name in EXPECTED_TOOLS:
        document = yaml.safe_load((ROOT / "tools" / f"{name}.yaml").read_text(encoding="utf-8"))
        for parameter in document["parameters"]:
            assert parameter["form"] in {"llm", "form"}
            assert parameter.get("human_description")
            assert parameter.get("llm_description")
        if name in {"build_index", "structure_query"}:
            selector = next(item for item in document["parameters"] if item["name"] == "fallback_model")
            assert selector["type"] == "model-selector"
            assert selector["scope"] == "llm" and selector["form"] == "form"
        properties = document["output_schema"]["properties"]
        for field, field_type in expected_outputs[name].items():
            assert properties[field]["type"] == field_type
    result_item = yaml.safe_load((ROOT / "tools" / "search.yaml").read_text(encoding="utf-8"))["output_schema"]["properties"]["results"]["items"]
    assert result_item["type"] == "object"
    assert {"id", "score", "attributes", "raw", "reasons"} <= set(result_item["properties"])


def main() -> None:
    verify_runtime_contracts()
    verify_dify_registration()
    print("PLUGIN_RUNTIME_CONTRACTS_OK")


if __name__ == "__main__":
    main()

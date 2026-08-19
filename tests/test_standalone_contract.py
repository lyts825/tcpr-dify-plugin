import json
import os
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _hybrid_definition() -> dict:
    return {
        "primary_key": "id",
        "attributes": {
            "id": {"kind": "string"},
            "color": {
                "kind": "enum",
                "aliases": ["颜色"],
                "value_aliases": {"红": "red"},
            },
            "ram": {"kind": "numeric", "units": {"": 1}},
        },
    }


def _fake_session(outputs=None, calls=None, error=None):
    from dify_plugin.entities.workflow_node import NodeResponse

    calls = calls if calls is not None else []

    class Extractor:
        def invoke(self, parameters, model_config, query, instruction):
            calls.append((parameters, model_config, query, instruction))
            if error is not None:
                raise RuntimeError(error)
            return NodeResponse(
                process_data={},
                inputs={},
                outputs=outputs if outputs is not None else {},
            )

    return SimpleNamespace(
        workflow_node=SimpleNamespace(parameter_extractor=Extractor()),
    )


def _run_tool(tool_cls, tool_module, service, parameters, *, outputs=None, calls=None, error=None):
    previous = tool_module.service_for
    try:
        tool_module.service_for = lambda _tool: service
        tool = tool_cls.from_credentials({})
        tool.session = _fake_session(outputs, calls, error)
        return list(tool.invoke(parameters))[-1].message.json_object
    finally:
        tool_module.service_for = previous


def test_provider_exposes_exactly_four_tools():
    text = (ROOT / "provider" / "tcpr.yaml").read_text(encoding="utf-8")
    tools = [line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- tools/")]
    assert tools == [
        "tools/search.yaml",
        "tools/structure_query.yaml",
        "tools/build_index.yaml",
        "tools/build_database.yaml",
    ]


def test_shared_core_is_bundled_only():
    text = (ROOT / "tcpr_core" / "shared_core.py").read_text(encoding="utf-8")
    assert "bundled.tcpr" in text
    assert "src/tcpr" not in text
    assert "sync_plugin_core" not in text


def test_old_public_tool_descriptors_are_absent():
    tools = ROOT / "tools"
    for name in ("get_schema", "import_products", "rebuild_index", "tcpr_search"):
        assert not (tools / f"{name}.py").exists()
        assert not (tools / f"{name}.yaml").exists()


def test_manual_index_tool_yaml_contract():
    index_yaml = (ROOT / "tools" / "build_index.yaml").read_text(encoding="utf-8")
    assert "name: index_json" in index_yaml
    assert "name: file" not in index_yaml
    assert "name: primary_key" not in index_yaml
    assert "does not read a data file" in index_yaml

    database_yaml = (ROOT / "tools" / "build_database.yaml").read_text(encoding="utf-8")
    assert "name: file" in database_yaml
    assert "name: index_id" in database_yaml
    assert "name: primary_key" not in database_yaml


def test_bundled_runtime_uses_manual_index_definition():
    from tcpr_core.shared_core import CoreService, InMemoryKV

    definition = {
        "primary_key": "id",
        "attributes": {
            "id": {"kind": "string"},
            "color": {"kind": "enum"},
        },
    }
    source = {
        "data": json.dumps([{"id": "a", "color": "red"}]).encode(),
        "filename": "records.json",
    }
    service = CoreService(InMemoryKV())
    index_id = service.build_index(json.dumps(definition))
    assert "postings" not in service.store.load("index", index_id)
    database_id = service.build_database(source, index_id)
    result = service.search(
        {"hard": [{"attr": "color", "op": "EQ", "value": "red"}]},
        index_id,
        database_id,
    )
    assert result["status"] == "OK"
    assert [row["id"] for row in result["results"]] == ["a"]


def test_structure_query_is_deterministic_and_search_compatible():
    from tcpr_core.shared_core import CoreService, InMemoryKV

    definition = {
        "primary_key": "id",
        "attributes": {
            "id": {"kind": "string"},
            "color": {
                "kind": "enum",
                "aliases": ["颜色"],
                "value_aliases": {"红色": "red"},
            },
            "count": {"kind": "numeric", "units": {"": 1}},
        },
    }
    service = CoreService(InMemoryKV())
    index_id = service.build_index(json.dumps(definition, ensure_ascii=False))
    first = service.structure_query("颜色=红色 且 count>=2", index_id)
    second = service.structure_query("颜色=红色 且 count>=2", index_id)
    assert first == second
    document = json.loads(first["query_json"])
    assert document == {
        "hard": [
            {"attr": "color", "op": "EQ", "value": "red"},
            {"attr": "count", "op": "GE", "value": 2},
        ],
        "soft": [],
    }


def test_dify_registration_loads_manifest_provider_and_all_tools():
    from dify_plugin import DifyPluginEnv
    from dify_plugin.core.plugin_registration import PluginRegistration

    previous = Path.cwd()
    try:
        os.chdir(ROOT)
        registration = PluginRegistration(DifyPluginEnv())
    finally:
        os.chdir(previous)
    assert registration.configuration.version == "0.0.4"
    assert sorted(registration.tools_mapping["tcpr"][2]) == [
        "build_database",
        "build_index",
        "search",
        "structure_query",
    ]


def test_dify_workflow_yaml_contract_is_llm_bindable_and_typed():
    expected = {
        "search": {"count": "integer", "results": "array", "debug": "object", "error": "object"},
        "structure_query": {"query": "object", "parse_source": "string", "error": "object"},
        "build_index": {"parse_source": "string", "index_json": "string", "index_definition": "object", "error": "object"},
        "build_database": {"error": "object"},
    }
    for name, properties in expected.items():
        document = yaml.safe_load((ROOT / "tools" / f"{name}.yaml").read_text(encoding="utf-8"))
        for parameter in document["parameters"]:
            assert parameter["form"] in {"llm", "form"}
            assert parameter.get("human_description")
            assert parameter.get("llm_description")
        if name in {"build_index", "structure_query"}:
            selector = next(item for item in document["parameters"] if item["name"] == "fallback_model")
            assert selector["type"] == "model-selector"
            assert selector["scope"] == "llm" and selector["form"] == "form"
        output = document["output_schema"]["properties"]
        for field, field_type in properties.items():
            assert output[field]["type"] == field_type
        if name == "search":
            result_item = output["results"]["items"]
            assert result_item["type"] == "object"
            assert {"id", "score", "attributes", "raw", "reasons"} <= set(result_item["properties"])


def test_dify_tool_failure_payloads_keep_error_objects():
    from tools.build_database import BuildDatabaseTool
    from tools.build_index import BuildIndexTool
    from tools.search import SearchTool
    from tools.structure_query import StructureQueryTool

    for tool_cls in (BuildIndexTool, BuildDatabaseTool, SearchTool, StructureQueryTool):
        messages = list(tool_cls.from_credentials({}).invoke({}))
        payload = messages[-1].message.json_object
        assert isinstance(payload["error"], dict)
        assert set(payload["error"]) == {"code", "message"}


def test_build_index_hybrid_routing_priority_and_deterministic_path():
    from tcpr_core.shared_core import CoreService, InMemoryKV
    import tools.build_index as tool_module
    from tools.build_index import BuildIndexTool

    definition = _hybrid_definition()
    service = CoreService(InMemoryKV())
    calls = []
    valid_requirement = "primary_key: id\nattributes:\n id: string\n color: enum\n ram: numeric units=元:1"
    manual = json.dumps(definition, ensure_ascii=False)

    payload = _run_tool(
        BuildIndexTool,
        tool_module,
        service,
        {
            "index_json": manual,
            "index_requirement": "this conflicting requirement must be ignored",
            "fallback_model": {"provider": "fake", "model": "fake-model"},
        },
        outputs={"index_json": manual},
        calls=calls,
    )
    assert payload["status"] == "OK"
    assert payload["parse_source"] == "manual"
    assert calls == []
    assert service.get_index_definition(payload["index_id"])["index_definition"] == definition

    service = CoreService(InMemoryKV())
    calls = []
    payload = _run_tool(
        BuildIndexTool,
        tool_module,
        service,
        {
            "index_json": "not valid JSON",
            "index_requirement": valid_requirement,
            "fallback_model": {"provider": "fake", "model": "fake-model"},
        },
        outputs={"index_json": manual},
        calls=calls,
    )
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert calls == []
    assert service.store.active("index") is None

    service = CoreService(InMemoryKV())
    calls = []
    payload = _run_tool(
        BuildIndexTool,
        tool_module,
        service,
        {"index_requirement": valid_requirement, "fallback_model": {"provider": "fake", "model": "fake-model"}},
        outputs={"index_json": manual},
        calls=calls,
    )
    assert payload["status"] == "OK"
    assert payload["parse_source"] == "deterministic"
    assert calls == []


def test_build_index_model_fallback_once_and_invalid_output_not_saved():
    from tcpr_core.shared_core import CoreService, InMemoryKV
    import tools.build_index as tool_module
    from tools.build_index import BuildIndexTool

    manual = json.dumps(_hybrid_definition(), ensure_ascii=False)
    service = CoreService(InMemoryKV())
    calls = []
    payload = _run_tool(
        BuildIndexTool,
        tool_module,
        service,
        {
            "index_requirement": "please infer an index from this prose",
            "fallback_model": {"provider": "fake", "model": "fake-model", "mode": "chat", "completion_params": {}},
        },
        outputs={"index_json": "```json\n" + manual + "\n```"},
        calls=calls,
    )
    assert payload["status"] == "OK" and payload["parse_source"] == "model"
    assert len(calls) == 1
    parameters, model_config, query, instruction = calls[0]
    assert parameters[0].name == "index_json"
    assert model_config.provider == "fake" and model_config.name == "fake-model"
    assert query == "please infer an index from this prose"
    assert "product" not in instruction.lower() and "database" not in instruction.lower()

    service = CoreService(InMemoryKV())
    calls = []
    payload = _run_tool(
        BuildIndexTool,
        tool_module,
        service,
        {
            "index_requirement": "please infer an index from this prose",
            "fallback_model": {"provider": "fake", "model": "fake-model"},
        },
        outputs={"index_json": "{\"primary_key\":\"id\",\"attributes\":{\"id\":{\"kind\":\"numeric\"}}}"},
        calls=calls,
    )
    assert payload["error"]["code"] == "MODEL_OUTPUT_INVALID"
    assert len(calls) == 1
    assert service.store.active("index") is None


def test_structure_query_hybrid_routing_search_compatibility_and_no_repair_for_json():
    from tcpr_core.shared_core import CoreService, InMemoryKV
    import tools.structure_query as tool_module
    from tools.structure_query import StructureQueryTool

    service = CoreService(InMemoryKV())
    index_id = service.build_index(json.dumps(_hybrid_definition(), ensure_ascii=False))
    database_id = service.build_database(
        {"data": json.dumps([
            {"id": "a", "color": "red", "ram": 16},
            {"id": "b", "color": "blue", "ram": 8},
        ]).encode(), "filename": "rows.json"},
        index_id,
    )

    calls = []
    payload = _run_tool(
        StructureQueryTool,
        tool_module,
        service,
        {"requirement": "颜色=红 且 ram>=16", "index_id": index_id, "fallback_model": {"provider": "fake", "model": "fake-model"}},
        outputs={"query_json": "{}"},
        calls=calls,
    )
    assert payload["status"] == "OK" and payload["parse_source"] == "deterministic"
    assert calls == []

    model_query = {"hard": [
        {"attr": "color", "op": "EQ", "value": "red"},
        {"attr": "ram", "op": "GE", "value": 16},
    ]}
    calls = []
    payload = _run_tool(
        StructureQueryTool,
        tool_module,
        service,
        {"requirement": "find a red product with at least sixteen ram", "index_id": index_id, "fallback_model": {"provider": "fake", "model": "fake-model"}},
        outputs={"query_json": json.dumps(model_query)},
        calls=calls,
    )
    assert payload["status"] == "OK" and payload["parse_source"] == "model" and len(calls) == 1
    assert service.search(payload["query_json"], index_id, database_id)["results"][0]["id"] == "a"
    assert "database rows" not in calls[0][3].lower() and '"id":"a"' not in calls[0][3]

    calls = []
    payload = _run_tool(
        StructureQueryTool,
        tool_module,
        service,
        {"requirement": "{bad json", "index_id": index_id, "fallback_model": {"provider": "fake", "model": "fake-model"}},
        outputs={"query_json": json.dumps(model_query)},
        calls=calls,
    )
    assert payload["error"]["code"] == "INVALID_QUERY" and calls == []


def test_structure_query_invalid_model_output_and_model_errors_are_stable_and_redacted():
    from tcpr_core.shared_core import CoreService, InMemoryKV
    import tools.structure_query as tool_module
    from tools.structure_query import StructureQueryTool

    service = CoreService(InMemoryKV())
    index_id = service.build_index(json.dumps(_hybrid_definition(), ensure_ascii=False))
    calls = []
    payload = _run_tool(
        StructureQueryTool,
        tool_module,
        service,
        {"requirement": "unstructured natural language", "index_id": index_id, "fallback_model": {"provider": "fake", "model": "fake-model"}},
        outputs={"query_json": json.dumps({"hard": [{"attr": "unknown", "op": "EQ", "value": "x"}]})},
        calls=calls,
    )
    assert payload["error"]["code"] == "MODEL_OUTPUT_INVALID" and len(calls) == 1

    calls = []
    payload = _run_tool(
        StructureQueryTool,
        tool_module,
        service,
        {"requirement": "another unstructured requirement", "index_id": index_id, "fallback_model": {"provider": "fake", "model": "fake-model"}},
        calls=calls,
        error="SECRET_MODEL_FAILURE",
    )
    assert payload["error"]["code"] == "MODEL_INVOCATION_FAILED"
    assert "SECRET_MODEL_FAILURE" not in payload["error"]["message"]
    assert len(calls) == 1

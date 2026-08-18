from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_provider_exposes_exactly_three_tools():
    text = (ROOT / "provider" / "tcpr.yaml").read_text(encoding="utf-8")
    tools = [line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- tools/")]
    assert tools == ["tools/search.yaml", "tools/build_index.yaml", "tools/build_database.yaml"]


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

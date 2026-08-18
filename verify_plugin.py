"""Offline runtime and packaging checks for the standalone Dify plugin.

This script intentionally uses only the plugin checkout.  It validates the
three public provider tools and exercises the bundled core through the same
``tcpr_core.shared_core`` adapter used by Dify.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from tcpr_core.shared_core import CoreService, InMemoryKV


ROOT = Path(__file__).resolve().parent
EXPECTED_TOOLS = {"search", "build_index", "build_database"}


def verify_runtime_contracts() -> None:
    provider = (ROOT / "provider" / "tcpr.yaml").read_text(encoding="utf-8")
    for name in EXPECTED_TOOLS:
        assert f"tools/{name}.yaml" in provider, f"missing provider tool: {name}"
    assert provider.count("tools/") == 3, "provider must expose exactly three tools"
    assert not any(
        old in provider for old in ("get_schema", "import_products", "rebuild_index", "tcpr_search")
    )

    manifest = (ROOT / "manifest.yaml").read_text(encoding="utf-8")
    assert "version: 0.0.2" in manifest and "version: '3.12'" in manifest
    assert (ROOT / "tcpr_core" / "bundled" / "tcpr" / "core_api.py").is_file()
    shared = (ROOT / "tcpr_core" / "shared_core.py").read_text(encoding="utf-8")
    assert "bundled.tcpr" in shared
    assert "sync_plugin_core" not in shared and "src/tcpr" not in shared

    data = io.StringIO()
    writer = csv.DictWriter(data, fieldnames=["id", "color", "count"])
    writer.writeheader()
    writer.writerow({"id": "a", "color": "red", "count": "2"})
    writer.writerow({"id": "b", "color": "blue", "count": "5"})
    source = {"data": data.getvalue().encode(), "filename": "records.csv"}
    service = CoreService(InMemoryKV())
    index_id = service.build_index(source)
    database_id = service.build_database(source, index_id)
    result = service.search(
        json.dumps({"hard": [{"attr": "color", "op": "EQ", "value": "red"}]}),
        index_id,
        database_id,
    )
    assert result["status"] == "OK", result
    assert [row["id"] for row in result["results"]] == ["a"], result


def main() -> None:
    verify_runtime_contracts()
    print("PLUGIN_RUNTIME_CONTRACTS_OK")


if __name__ == "__main__":
    main()

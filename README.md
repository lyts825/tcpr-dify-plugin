# TCPR Dify Tool Plugin

TCPR (Typed Constraint-Preserving Retrieval) is a Dify Tool Plugin for exact
retrieval over dynamic product attributes. The plugin exposes exactly three tools:

- `search` — execute typed hard/soft constraints against a persisted index/database.
- `build_index` — build and atomically activate an index from CSV/XLSX/JSON/JSONL.
- `build_database` — build a matching database snapshot with typed missing markers.

The provider is a thin adapter over the bundled core in
`tcpr_core/bundled/tcpr/core_api.py`; that bundled copy is authoritative at
runtime and makes the plugin independently installable. `build_index` returns `index_id`; pass the same source file and ID to
`build_database`, then pass both IDs to `search`. Search consumes the persisted
index and does not rebuild it per request. The optional `primary_key` field can
be supplied; otherwise `id`, `product_id`, `product_code`, `sku`, `编号`, or
`产品编号` is inferred and must be unique.

The provider identifier is `lyts825/tcpr/tcpr`.

## Install from GitHub

This repository is intended to be installed through Dify's GitHub installer.
The repository must have a published GitHub Release with a `.difypkg` asset.

1. Open **Plugins** in Dify.
2. Select **Install Plugin** and choose **From GitHub**.
3. Enter `https://github.com/lyts825/tcpr-dify-plugin`.
4. Select the release matching the plugin version, then confirm the requested
   permissions.

For version `0.0.2`, the release tag is `v0.0.2` and the asset is
`tcpr-0.0.2.difypkg`. For each update, increment `version` in `manifest.yaml`,
package the plugin again, and publish a new matching release.

## Data and runtime

The plugin runs with Python 3.12 and uses Dify persistent storage for validated
product snapshots, indexes, schema metadata, and import warnings. It does not
call an external service or send product rows to a third party. Any external
knowledge-base binding is configured by the target Dify workspace and is not
bundled with this repository.

The plugin requests 256 MiB of persistent storage. Actual availability depends
on the Dify runtime. Unsigned packages may require administrator approval on
self-hosted Dify instances with third-party signature verification enabled.

## Local development

From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -B -c "from verify_plugin import verify_runtime_contracts; verify_runtime_contracts(); print('RUNTIME_CONTRACTS_OK')"
```

The full verification script additionally requires a user-provided production
workbook; that data is intentionally not included in the repository or plugin
package.

To create a distributable package, run the official Dify Plugin CLI from this
project directory:

```powershell
dify plugin package . -o .\dist\tcpr-0.0.2.difypkg
```

## Privacy and support

See [PRIVACY.md](PRIVACY.md) for the data-handling policy. Please use the
[GitHub Issues](https://github.com/lyts825/tcpr-dify-plugin/issues) page for
support and bug reports.

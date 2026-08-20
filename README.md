# TCPR Dify Tool Plugin

> The default `main` branch is the **composable tool-suite edition** for users
> who want explicit control over each workflow step. For the lower-learning-cost
> single-tool experience, use the `codex/remote-query` branch. See
> [BRANCHES.md](BRANCHES.md) for a side-by-side comparison.

TCPR (Typed Constraint-Preserving Retrieval) is a Dify Tool Plugin for exact
retrieval over dynamic product attributes. The plugin exposes exactly four tools:

- `search` — execute typed hard/soft constraints against a persisted index/database.
- `structure_query` — deterministically convert a schema-aware requirement into validated query JSON, with one optional model fallback for failed natural language only.
- `build_index` — save and atomically activate a user-authored logical index definition.
- `build_database` — build a matching database snapshot with typed missing markers.

The provider is a thin adapter over the bundled core in
`tcpr_core/bundled/tcpr/core_api.py`; that bundled copy is authoritative at
runtime and makes the plugin independently installable. `build_index` accepts
authoritative `index_json` or a documented English/Chinese `index_requirement`.
A non-empty `index_json` is validated directly and never replaced by the
requirement or a model. If the DSL fails, the optional user-selected Dify
Parameter Extractor is called at most once; its candidate is validated by the
bundled core before saving. The tool does not read a data file. It returns
`index_id` and `parse_source`; pass a data file and that ID to
`build_database`, which validates and normalizes rows and generates the
persistent postings/ranges physical index. Then pass both IDs to `search`.
Search consumes the persisted database index and does not rebuild it per request.
`structure_query` uses the referenced index definition to parse text or validate
an existing JSON query. Only failed, non-empty natural language may use the
optional one-call model fallback; explicit JSON/dict input, missing indexes,
storage errors, and corrupted definitions never use it. Model candidates are
validated by the core before being returned.

The DSL is intentionally strict: `primary_key: id` followed by
`attributes:`/`fields:` and declarations such as `id: string`,
`price: numeric units=元:1`, or
`color: enum aliases=颜色 value_aliases=红色->red`. Every field kind is
explicit; units are accepted only when declared and are never inferred; optional `enum_order` is required for
`ordered_enum`. Unknown statements or trailing text are errors.

The provider identifier is `lyts825/tcpr/tcpr`.

## Install from GitHub

This repository is intended to be installed through Dify's GitHub installer.
The repository must have a published GitHub Release with a `.difypkg` asset.

1. Open **Plugins** in Dify.
2. Select **Install Plugin** and choose **From GitHub**.
3. Enter `https://github.com/lyts825/tcpr-dify-plugin`.
4. Select the release matching the plugin version, then confirm the requested
   permissions.

For version `0.1.0`, the release tag is `v0.1.0` and the asset is
`tcpr-0.1.0.difypkg`. For each update, increment `version` in `manifest.yaml`,
package the plugin again, and publish a new matching release.

## Data and runtime

The plugin runs with Python 3.12 and uses Dify persistent storage for validated
product snapshots, indexes, schema metadata, and import warnings. Except for the
single fallback explicitly selected by the user, it does not call an external
service or send product rows to a third party. The production Product_KB binding
is configured by the target Dify workspace and is not bundled with this
repository.

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

The verification script exercises the local runtime contracts and does not
require a production workbook. Production data is intentionally not included in
the repository or plugin package.

To create a distributable package, run the official Dify Plugin CLI from this
project directory:

```powershell
dify plugin package . -o .\dist\tcpr-0.1.0.difypkg
```

## Privacy and support

See [PRIVACY.md](PRIVACY.md) for the data-handling policy. Please use the
[GitHub Issues](https://github.com/lyts825/tcpr-dify-plugin/issues) page for
support and bug reports.

# TCPR Dify Tool Plugin

TCPR (Typed Constraint-Preserving Retrieval) is a Dify Tool Plugin for exact
retrieval over sparse product attributes. The plugin provides four tools:

- `tcpr_search` — parse and evaluate typed hard/soft constraints.
- `import_products` — validate and import a CSV or XLSX product file.
- `rebuild_index` — rebuild the current product index safely.
- `get_schema` — inspect the imported product schema and index capabilities.

The provider identifier is `lyts825/tcpr/tcpr`.

## Install from GitHub

This repository is intended to be installed through Dify's GitHub installer.
The repository must have a published GitHub Release with a `.difypkg` asset.

1. Open **Plugins** in Dify.
2. Select **Install Plugin** and choose **From GitHub**.
3. Enter `https://github.com/lyts825/tcpr-dify-plugin`.
4. Select the release matching the plugin version, then confirm the requested
   permissions.

For version `0.0.1`, the release tag is `v0.0.1` and the asset is
`tcpr-0.0.1.difypkg`. For each update, increment `version` in `manifest.yaml`,
package the plugin again, and publish a new matching release.

## Data and runtime

The plugin runs with Python 3.12 and uses Dify persistent storage for validated
product snapshots, indexes, schema metadata, and import warnings. It does not
call an external service or send product rows to a third party. The production
Product_KB binding is configured by the target Dify workspace and is not bundled
with this repository.

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

To create a distributable package, run the official Dify Plugin CLI from the
directory above this project:

```powershell
dify plugin package .\tcpr-dify-plugin -o .\dist\tcpr-0.0.1.difypkg
```

## Privacy and support

See [PRIVACY.md](PRIVACY.md) for the data-handling policy. Please use the
[GitHub Issues](https://github.com/lyts825/tcpr-dify-plugin/issues) page for
support and bug reports.

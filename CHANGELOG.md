# Changelog

All notable changes to this Dify plugin are documented here.

## 0.0.9 - 2026-08-20

### Fixed

- Made `remote_query` compatible with Dify's source-module loader on Python
  3.12. The module no longer uses postponed annotations, which caused Python's
  `dataclass` decorator to fail before the tool could start.

### Distribution

- Designated `codex/remote-query` as the one-click remote-query branch; the
  default `main` branch carries the composable multi-tool edition.

## 0.0.8 - 2026-08-20

### Breaking changes

- Replaced `search`, `structure_query`, `build_index`, and `build_database`
  with one `remote_query` tool.
- Removed the plugin's local index, database snapshot, and storage permission.
  Existing local data is not migrated.

### Added

- Read-only PostgreSQL and MySQL queries with bounded results and timeout
  limits.
- Default TCPR constraint compilation against a form-provided column allowlist;
  an explicitly selected bounded `raw_sql` mode remains available.
- TLS controls, secret-input password handling, stable redacted errors, and
  expanded setup and privacy documentation.

### Migration

Update each existing workflow to use `remote_query`. Configure a dedicated
least-privilege read-only database account, the connection form fields, and a
TCPR table/schema allowlist. Review all downstream nodes before passing query
results to an LLM or other external service.

# Changelog

All notable changes to the composable TCPR edition are documented here.

## 0.1.0 - 2026-08-21

### Changed

- Established `main` as the composable multi-tool release track.
- Exposed `build_index`, `build_database`, `structure_query`, and `search` as
  separate tools so users can arrange and reuse workflow steps independently.
- Moved the one-click read-only PostgreSQL/MySQL experience to the
  `codex/remote-query` branch.

### Migration

Users who prefer the former single `remote_query` entry point should install
the `codex/remote-query` edition. The two editions share a plugin identity and
should not be installed together in the same Dify workspace.

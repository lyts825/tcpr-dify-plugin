# TCPR Dify Tool Plugin

> This is the **one-click remote-query edition** on the
> `codex/remote-query` branch. It exposes one tool and is intended for users
> who prefer the shortest setup and learning path. The default `main` branch
> provides the composable four-tool edition for flexible workflows. See
> [BRANCHES.md](BRANCHES.md) before choosing an edition.

TCPR (`lyts825/tcpr`) is a Dify tool plugin that exposes one operation:
`remote_query`. It connects to a PostgreSQL or MySQL database configured for
the current call and returns a bounded, JSON-serializable result. Connection,
table, and TCPR schema fields are form inputs; the model supplies only a
constraint query JSON.

TCPR mode is the default. It maps an explicit allowlisted schema to one table,
compiles hard constraints into an index-friendly parameterized `WHERE`, and
compiles weighted soft constraints into server-side `SUM(CASE ...)` scoring.
Results are ordered by score descending and the configured primary key
ascending, with a server-side `LIMIT top_k + 1`. An explicitly selected
`raw_sql` mode remains available for bounded read-only SQL and wraps it in an
outer `LIMIT 101`.

The SQL policy is intentionally narrow: one `SELECT` or read-only `WITH`
statement, with named placeholders such as `:status`. Multiple statements,
writes, DDL/DCL, mutating CTEs, `SELECT INTO`/`OUTFILE`, and locking clauses are
rejected before the driver is called. The plugin starts a server-enforced
read-only transaction, uses a 10 second connection timeout and a 30 second
query timeout, and returns at most 100 rows (hard limits: 30/60 seconds and
1000 rows). A result over the limit is marked `truncated`.

Configure a least-privilege read-only database account. TLS certificate
verification (`verify-full`) is the default. Passwords and connection details
are never returned in errors or intentionally logged by the plugin. The
password is a Dify `secret-input` field; Dify handles any configured-secret
storage under the policies of the deployment. Query rows return to the calling
workflow and may be provided to downstream LLM nodes if that workflow is
configured to do so. Sensitive-looking result fields are redacted during
serialization, but workflows should select only the data they are allowed to
use.

## Tool inputs

`query_mode` is a `form` parameter defaulting to `tcpr`; choose `raw_sql`
explicitly to enable the raw branch. `database_type`, `host`, `port`,
`database`, `username`, `password`, `ssl_mode`, `table`, and
`tcpr_schema_json` are form parameters. The schema JSON has a `fields` object
whose entries contain `column` and TCPR `kind` (`numeric`, `enum`, `multi`,
etc.); declare exactly one `primary_key` for stable ordering. Example:

```json
{"primary_key":"id","fields":{"id":{"column":"id","kind":"numeric"},"price":{"column":"price","kind":"numeric"},"features":{"column":"features","kind":"multi"}}}
```

`tcpr_query_json` is an LLM parameter with `hard`, optional weighted `soft`,
`select`, and `top_k`, for example:

```json
{"hard":[{"attr":"price","op":"GE","value":100}],"soft":[{"constraint":{"attr":"features","op":"CONTAINS","value":"wifi6"},"weight":2}],"select":["id","price"],"top_k":20}
```

`sql` and `parameters_json` are only used in explicitly selected `raw_sql`
mode. Values are always bound parameters; table/column identifiers come only
from the form-provided allowlist. TCPR NULL/missing values retain three-valued
logic, and contradictory hard constraints return without opening a connection.

## Setup and authorization

1. Install the packaged plugin in Dify and add `TCPR Remote Read-only Query`
   to a workflow or agent.
2. Create a dedicated database account with access limited to the required
   database, schema/table, and `SELECT` operation. Do not reuse an owner,
   administrator, or write-capable account.
3. In the tool's form fields, set the database type, host, port, database,
   username, password, and TLS mode. The default `verify-full` requires a
   certificate that the plugin runtime can validate; use `require` or `disable`
   only when the deployment's network-security policy explicitly permits it.
4. For TCPR mode, set `table` and `tcpr_schema_json` as a strict column
   allowlist with one `primary_key`. Keep `query_mode` set to `tcpr` unless a
   reviewed workflow explicitly needs bounded `raw_sql`.
5. Configure downstream workflow nodes so that only authorized recipients can
   receive database rows. Test with non-production data before production use.

The plugin opens a connection only to the database endpoint configured in the
form. See [PRIVACY.md](PRIVACY.md) for the full data-flow and retention notice.

## Remote index recommendations

The plugin never creates or changes tables, indexes, schemas, or other remote
database objects. For frequent TCPR workloads, a database administrator may
consider PostgreSQL B-tree indexes or MySQL BTREE indexes on commonly used
hard `EQ`, `IN`, and range-filter columns. For multi-valued JSON arrays,
PostgreSQL GIN indexes with `jsonb_path_ops` or MySQL multi-valued JSON
indexes may help, subject to the database version and the administrator's
indexing policy. A composite index covering frequent filter columns and the
primary-key ordering can also be considered after reviewing real query plans.
These are operational recommendations only; the plugin does not run
`EXPLAIN`, create indexes, or modify remote state.

## Installation and verification

The package requires Python 3.12, `pg8000` for PostgreSQL, and `PyMySQL` for
MySQL:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -B verify_plugin.py
```

The tests use fake drivers and do not connect to a real external database.
Package with the Dify CLI:

```powershell
dify plugin package . -o .\dist\tcpr-0.0.9.difypkg
```

See [PRIVACY.md](PRIVACY.md) for data handling.

## Breaking change in 0.0.8

Version 0.0.8 replaces the earlier local-index tools (`search`,
`structure_query`, `build_index`, and `build_database`) with the single
`remote_query` tool. Existing workflows must be updated to configure a
read-only PostgreSQL or MySQL connection and use either TCPR JSON (default) or
the explicitly selected bounded `raw_sql` mode. No upgrade path preserves the
removed local storage or index data.

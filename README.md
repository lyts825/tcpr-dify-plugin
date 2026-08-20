# TCPR Dify Tool Plugin

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
are used only during the invocation; they are not written to Dify storage,
returned in errors, sent to an LLM, or logged. Sensitive-looking result fields
are redacted during serialization.

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
dify plugin package . -o .\dist\tcpr-0.0.8.difypkg
```

See [PRIVACY.md](PRIVACY.md) for data handling.

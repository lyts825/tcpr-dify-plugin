# Privacy

TCPR does not build or persist a local index, database snapshot, schema, or
uploaded file. Each `remote_query` call receives its connection fields and
uses them only to open one PostgreSQL or MySQL read-only transaction. The
password and connection details are never stored in Dify storage, returned in
the tool payload, sent to a model, or intentionally logged.

The table and TCPR schema are form-provided allowlists and are never generated
by the model. In the default TCPR mode, only the model's constraint JSON is
accepted; its values become bound parameters and its identifiers are resolved
against that allowlist. The optional raw SQL branch is available only when the
form explicitly selects `raw_sql`, and is wrapped with a server-side row cap.
The plugin accepts one bounded read-only query and serializes rows without
exposing driver exception text. Sensitive-looking result column names are
redacted. TLS verification is enabled by default, and database administrators
should grant the account the minimum read-only permissions needed by the
workflow.

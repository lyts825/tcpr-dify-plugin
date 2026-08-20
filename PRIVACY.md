# Privacy

## Scope and controller

TCPR is a connector to a database selected and controlled by the Dify instance
administrator. The administrator is responsible for ensuring that they have a
lawful basis to query that database and for configuring the Dify workflow that
consumes the result. TCPR does not operate an independent service, account
system, analytics service, or telemetry endpoint.

## Data handled and where it goes

For each `remote_query` invocation, TCPR receives connection information
(database type, host, port, database name, username, and password), the table
and allowlisted schema, and the query parameters. Any of these values, and the
queried database rows, may contain personal or sensitive data depending on the
database chosen by the administrator.

The plugin uses the connection information and query only to open one
read-only PostgreSQL or MySQL transaction against that administrator-configured
database. The database server is the only external destination contacted by the
plugin. TCPR does not send this information to a TCPR-operated third party,
advertising provider, analytics provider, or model provider.

Query results are returned to the Dify workflow. The workflow administrator
controls any downstream nodes; for example, an Agent or LLM node may receive
the returned rows if it is configured to do so. Configure least-privilege
database access, select only necessary columns, and avoid returning personal
data to downstream nodes unless that use is intended.

## Storage, retention, and security

TCPR itself does not build or persist a local index, database snapshot, schema,
uploaded file, connection record, query log, or telemetry record. It does not
include connection details or driver exception text in its tool payload or
intentional logs. The password field is declared as Dify `secret-input`; Dify
handles any storage of that configuration under the policies and deployment
settings of the Dify instance.

The form-provided table and TCPR schema are allowlists and are never generated
by the model. In the default TCPR mode, model-supplied constraint values become
bound SQL parameters and identifiers are resolved only against that allowlist.
The optional `raw_sql` branch requires an explicit form selection, accepts one
bounded read-only statement, and applies a server-side row cap. Sensitive-
looking result column names are redacted. TLS verification is enabled by
default; database administrators should grant the account only the minimum
read-only permissions required by the workflow.

## Contact and deletion requests

TCPR retains no data of its own, so it cannot independently delete database or
Dify records. For support, security reports, or requests about data held by a
deployment, contact the repository maintainer through
https://github.com/lyts825/tcpr-dify-plugin/issues and the administrator of
the relevant Dify instance or database.

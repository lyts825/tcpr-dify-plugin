# Privacy

TCPR processes product files supplied by the Dify tool caller. It stores only the
validated product snapshot, indexes, schema metadata, and import warnings in the
plugin's configured persistent storage. It does not call an external service or
send product rows to a third party. TCPR does not intentionally collect personal
data or telemetry. If a caller includes personal data in an uploaded product
file, that data remains subject to the target Dify workspace's storage and
retention controls and is not sent to an external service by this plugin.
External knowledge-base bindings are outside this plugin.

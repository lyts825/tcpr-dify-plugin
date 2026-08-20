# Privacy

TCPR processes product files supplied by the Dify tool caller. It stores only the
validated product snapshot, indexes, schema metadata, and import warnings in the
plugin's configured persistent storage. Except for the single fallback explicitly
selected by the user, it does not call an external service or send product rows,
database rows, search results, or credentials to a model.

When deterministic parsing fails and the caller supplied a fallback model,
Dify's Parameter Extractor sends only the requirement and the necessary index
definition to that user-selected model. This fallback is attempted at most
once; raw prompts and model output are not logged or persisted. TCPR does not
intentionally collect personal data or telemetry. External knowledge-base
bindings are outside this plugin.

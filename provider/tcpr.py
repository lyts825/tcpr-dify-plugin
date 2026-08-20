"""TCPR remote read-only database Provider entry point."""

from typing import Any

import tcpr_core.sdk_compat as _sdk_compat


class TcprProvider(_sdk_compat.ToolProvider):
    """Provider for the one public ``remote_query`` tool.

    Connection credentials are invocation parameters of the tool, not provider
    credentials.  The tool descriptor marks them as form fields and the tool
    never writes them to Dify storage.
    """

    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        """The provider intentionally has no persisted credential form."""
        if credentials:
            raise ValueError("TCPR provider has no persisted credentials")

"""Command-line client for operating a tai42 server over its HTTP API.

:func:`app_context` is the public seam for a command mounted from outside this
package (a server-side native command group) to read the invocation's
:class:`~tai42_cli.context.AppContext`.
"""

from tai42_cli.commands._common import app_context

__all__ = ["app_context"]

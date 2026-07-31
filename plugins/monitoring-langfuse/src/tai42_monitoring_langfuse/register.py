"""Self-register the Langfuse backend as the process monitoring provider.

Importing this module fires ``@register_monitoring``, which builds the backend
from the ``LANGFUSE_*`` environment; missing credentials raise (a selected
backend that can't build is a loud failure, not a silent downgrade).
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.monitoring import Monitoring

from tai42_monitoring_langfuse.factory import build_langfuse_backend


@tai42_app.monitoring.register_monitoring
def langfuse_monitoring() -> Monitoring:
    return build_langfuse_backend()

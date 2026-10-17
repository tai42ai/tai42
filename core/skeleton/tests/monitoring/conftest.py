"""Monitoring test wiring.

The observability router registers its routes through the ``tai42_app`` contract
handle at import time, exactly as external plugins do. Test modules import it at
collection, so bind the process app singleton first.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance

tai42_app.bind(instance.build_app())

"""The fleet-consistency suite: end-to-end proof that every worker converges after
every mutation path, that the boot rules refuse the unbootable configurations, and
that the historic fleet gaps stay closed under the app-owned worker bus.

Opt-in via ``TAI_E2E_FLEET`` (``collect_ignore_glob`` in the root conftest skips
``fleet/*`` when it is off) — the multi-worker / REPLICAS stacks this suite boots
carry a heavier CI cost. Scenarios land here in a later wave.
"""

from __future__ import annotations

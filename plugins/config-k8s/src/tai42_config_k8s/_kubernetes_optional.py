"""Guard for the optional ``kubernetes`` extra (used by K8sConfigManager)."""

from __future__ import annotations

import importlib.util


def require_kubernetes(*, extras: str = "tai42-config-k8s[k8s]") -> None:
    """Raise ImportError with a copy-pasteable install hint if ``kubernetes``
    is absent. Returns None on success; callers do their own import after."""
    if importlib.util.find_spec("kubernetes") is None:
        raise ImportError(f"K8s mode requires the 'kubernetes' package. Install with: pip install {extras}")

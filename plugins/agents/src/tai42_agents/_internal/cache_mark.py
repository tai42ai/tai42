"""The server-wide default system-prompt cache mark shared by every agent face.

Every face that can hold a stable system-prompt cache breakpoint resolves the
default the same way here: the mark form comes only from the kit provider
capability, so no provider name is named in this package.
"""

from __future__ import annotations

from typing import Any

from tai42_kit.llm.models import system_prompt_cache_mark

from tai42_agents.settings import agents_limits_settings


def default_system_cache_mark(provider: str) -> dict[str, Any] | None:
    """The system-prompt ``cache_control`` mark a face applies by default for ``provider``.

    ``None`` when the server-wide default
    (``TAI_AGENTS_SYSTEM_PROMPT_CACHE_DEFAULT``) is off or the provider takes no
    system-prompt cache mark; otherwise the mark kwargs the provider capability
    returns, merged onto the system message's text block. An unknown provider
    raises out of the capability, never a silent unmarked fallback.
    """
    if not agents_limits_settings().system_prompt_cache_default:
        return None
    return system_prompt_cache_mark(provider)

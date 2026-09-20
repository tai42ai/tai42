"""The ``claude_code`` agent package — Claude Code as a platform :class:`Agent`.

Importing this package registers ``ClaudeCodeAgent`` through ``tai42_app`` and binds the shared
hidden ``agent_resume`` continuation. The agent drives the real ``claude`` binary INSIDE a
sandbox session over a versioned JSONL exec protocol; the plugin server never imports the SDK
(only the runner payload, shipped as DATA and executed in-session, does).

Public surface:

* :class:`ClaudeCodeAgent` — the registered agent.
* :class:`ClaudeCodeSettings` / :func:`claude_code_settings` — operator settings
  (``TAI_AGENTS_CLAUDE_*``), including the exactly-one-auth rule and the digest-only image.
* :data:`SessionCredSpec` — the operator session-cred entry (static or connection-reference).
"""

from __future__ import annotations

from tai42_agents.claude_code.agent import ClaudeCodeAgent
from tai42_agents.claude_code.settings import (
    ClaudeCodeSettings,
    SessionCredSpec,
    claude_code_settings,
)

__all__ = [
    "ClaudeCodeAgent",
    "ClaudeCodeSettings",
    "SessionCredSpec",
    "claude_code_settings",
]

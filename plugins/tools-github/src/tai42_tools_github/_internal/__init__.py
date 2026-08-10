"""Private helper modules backing the GitHub provisioning tools.

Not part of the public API. ``tools`` holds the shared GitHub REST client that the thin registered
entrypoints in ``tai42_tools_github.tools`` delegate to. Nothing here registers through
``tai42_app``.
"""

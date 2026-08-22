"""Load the runner payload files this adapter ships as package DATA.

``runner_payload/`` is NOT an importable server module — it is the ONLY code that imports
``claude_agent_sdk``, and it runs INSIDE the session where the SDK is installed. It ships as
``*.tmpl`` DATA (so the server import graph and coverage never touch it) and is ``put_file``'d
into ``.runner/payload/`` EVERY TURN, where the ``.tmpl`` suffix is stripped to its real
filename (``tai_runner.py.tmpl`` -> ``tai_runner.py``, run as ``python -m tai_runner``).
"""

from __future__ import annotations

from importlib.resources import files

_PAYLOAD_ANCHOR = "tai42_agents.claude_code"
_PAYLOAD_DIRNAME = "runner_payload"
_TEMPLATE_SUFFIX = ".tmpl"


def runner_payload_files() -> list[tuple[str, bytes]]:
    """Every runner payload file as ``(in_session_filename, content)``, the ``.tmpl`` suffix
    stripped. Sorted for a deterministic authoring order."""
    root = files(_PAYLOAD_ANCHOR).joinpath(_PAYLOAD_DIRNAME)
    out: list[tuple[str, bytes]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_file() and entry.name.endswith(_TEMPLATE_SUFFIX):
            out.append((entry.name[: -len(_TEMPLATE_SUFFIX)], entry.read_bytes()))
    if not out:
        raise RuntimeError(f"no runner payload templates found under {_PAYLOAD_ANCHOR}/{_PAYLOAD_DIRNAME}")
    return out

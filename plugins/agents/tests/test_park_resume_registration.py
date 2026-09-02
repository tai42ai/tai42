"""The hidden park continuations register from the SHARED park machinery.

The park is unresumable unless the tools the platform and a nested driver fire —
``agent_resume`` for an answered ask, ``deliver_chained_park`` for a chained call's terminal —
are registered wherever a park-capable agent can park. That registration must therefore hold
for EVERY park-capable agent module on its own — not just the one whose import used to
carry it — because a deployment may load only one.

Each case runs in a FRESH interpreter (a subprocess), because module import is cached
process-wide: once one test here imports a park-capable module, a later import in the same
process re-runs nothing, so isolation is the only faithful way to prove "importing THIS
module alone registers the tool". The child binds a stub app whose ``tools.tool`` records
each bound name and RAISES the FastMCP duplicate-bind error on a re-bind — mirroring the
skeleton's ``on_duplicate="error"`` (a ``ValueError('Component already exists: ...')``) — so
the idempotent ``register_agent_resume_tool`` catches exactly that on the second caller and
the count still lands at exactly one binding.
"""

from __future__ import annotations

import subprocess
import sys

# Bind a stub app that records tool registrations and refuses duplicates (mirroring the
# skeleton's ``on_duplicate="error"``), import the modules named on argv, then print how
# many times the ``agent_resume`` tool registered.
_CHILD_PROGRAM = """
import importlib
import sys

from tai42_contract.app import tai42_app

_REGISTERED = []


class _StubAgents:
    def agent(self, name, tags=None, meta=None):
        def decorator(cls):
            return cls

        return decorator

    def get_agent(self, name):
        raise RuntimeError(name)


class _StubTools:
    def tool(self, *args, **kwargs):
        def decorator(func):
            name = kwargs.get("name") or getattr(func, "__name__", None)
            if name in _REGISTERED:
                raise ValueError("Component already exists: " + repr(name))
            _REGISTERED.append(name)
            return func

        if args and callable(args[0]):
            return decorator(args[0])
        return decorator


class _StubApp:
    agents = _StubAgents()
    tools = _StubTools()


tai42_app.bind(_StubApp())

for module_name in sys.argv[1:]:
    importlib.import_module(module_name)

from tai42_agents._internal.park.chain import CHAINED_PARK_DELIVERY_TOOL_NAME
from tai42_agents._internal.park.driver import AGENT_RESUME_TOOL_NAME

print("AGENT_RESUME_COUNT=" + str(_REGISTERED.count(AGENT_RESUME_TOOL_NAME)))
print("CHAINED_PARK_COUNT=" + str(_REGISTERED.count(CHAINED_PARK_DELIVERY_TOOL_NAME)))
"""


def _import_in_fresh_interpreter(*modules: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM, *modules],
        capture_output=True,
        text=True,
    )


def _registrations(result: subprocess.CompletedProcess[str], prefix: str) -> int:
    assert result.returncode == 0, (
        f"importing the agent module(s) failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, f"expected one count line, got: {result.stdout!r}"
    return int(lines[0].removeprefix(prefix))


def _resume_registrations(result: subprocess.CompletedProcess[str]) -> int:
    return _registrations(result, "AGENT_RESUME_COUNT=")


def _chained_registrations(result: subprocess.CompletedProcess[str]) -> int:
    return _registrations(result, "CHAINED_PARK_COUNT=")


def test_importing_tools_agent_alone_registers_agent_resume() -> None:
    result = _import_in_fresh_interpreter("tai42_agents.tools_agent")
    assert _resume_registrations(result) == 1


def test_importing_langchain_deep_agent_alone_registers_agent_resume() -> None:
    result = _import_in_fresh_interpreter("tai42_agents.langchain_deep_agent")
    assert _resume_registrations(result) == 1


def test_importing_both_park_capable_agents_registers_agent_resume_exactly_once() -> None:
    result = _import_in_fresh_interpreter("tai42_agents.tools_agent", "tai42_agents.langchain_deep_agent")
    assert _resume_registrations(result) == 1


def test_importing_tools_agent_alone_registers_the_chained_park_delivery() -> None:
    # A chaining loop's dispatches bind that tool as their delivery address, so a box loading
    # only this agent must still have it registered for the nested driver to fire.
    result = _import_in_fresh_interpreter("tai42_agents.tools_agent")
    assert _chained_registrations(result) == 1


def test_importing_langchain_deep_agent_alone_registers_the_chained_park_delivery() -> None:
    result = _import_in_fresh_interpreter("tai42_agents.langchain_deep_agent")
    assert _chained_registrations(result) == 1


def test_importing_both_chaining_agents_registers_the_chained_park_delivery_exactly_once() -> None:
    result = _import_in_fresh_interpreter("tai42_agents.tools_agent", "tai42_agents.langchain_deep_agent")
    assert _chained_registrations(result) == 1


def test_importing_claude_code_alone_registers_no_chained_park_delivery() -> None:
    # ``claude_code`` resumes by feeding its runner the answer to the interaction its pending
    # tool call waits on, so a park on the CALL is not a shape it can resume: it does not chain,
    # and it binds no delivery address for one.
    result = _import_in_fresh_interpreter("tai42_agents.claude_code")
    assert _resume_registrations(result) == 1
    assert _chained_registrations(result) == 0

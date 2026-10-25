"""The run-trace opener imports and runs with the LangChain and LangGraph stacks absent.

The subprocess hides every ``langchain*`` and ``langgraph*`` module with a meta-path finder that raises
``ModuleNotFoundError`` exactly as a missing install would.
"""

import subprocess
import sys
import textwrap

_CHILD = textwrap.dedent(
    """
    import sys

    class _Block:
        def find_spec(self, name, path=None, target=None):
            top = name.partition(".")[0]
            if top.startswith(("langchain", "langgraph")):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    sys.meta_path.insert(0, _Block())

    import tai42_kit.monitoring.run_trace as run_trace
    from tai42_contract.app import tai42_app

    class _Writer:
        def is_recording(self):
            return False

    class _Active:
        writer = _Writer()

    class _Facet:
        active = _Active()

    class _App:
        monitoring = _Facet()

    with tai42_app.bound(_App()), run_trace.open_run_trace() as root:
        assert root.context is None, root.context

    loaded = sorted(m for m in sys.modules if m.startswith(("langchain", "langgraph")))
    assert not loaded, loaded
    """
)


def test_the_opener_imports_and_runs_without_langchain() -> None:
    result = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

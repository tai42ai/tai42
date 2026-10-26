"""Every channel send in the skeleton goes through a site that writes its send record.

A ``.notify(`` or ``.deliver(`` call is a send on a channel. Each one sits in a function that
records the send around it; a new call anywhere else fails here, naming its file, line and function.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import tai42_skeleton

_SKELETON_SRC = Path(tai42_skeleton.__file__).parent

_SEND_METHODS = frozenset({"notify", "deliver"})

# The functions whose channel send writes a send record: the bridge reply loop, the refusal
# notice, the participant notices, ``notify_user`` and the ask delivery.
_RECORDED_SITES = frozenset(
    {
        ("conversations/delivery_channel.py", "_run_channel_send_loop"),
        ("conversations/delivery_channel.py", "_notify_refusal"),
        ("channels/participant_notice.py", "send_participant_notice.send"),
        ("channels/notify.py", "_send_with_telemetry.send"),
        ("interactions/ask/delivery.py", "_attempt_loop"),
    }
)


def _send_calls(tree: ast.Module) -> list[tuple[int, str]]:
    """Each ``.notify(`` / ``.deliver(`` call in ``tree`` with the dotted name of the function it sits in."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = (*scope, child.name)
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _SEND_METHODS
            ):
                found.append((child.lineno, ".".join(scope)))
            visit(child, inner)

    visit(tree, ())
    return found


def unrecorded_sends(modules: dict[str, ast.Module]) -> list[tuple[str, int, str]]:
    """The send calls in ``modules`` (keyed by path under the skeleton) outside every recorded site."""
    return [
        (path, line, function)
        for path, tree in sorted(modules.items())
        for line, function in _send_calls(tree)
        if (path, function) not in _RECORDED_SITES
    ]


def _skeleton_modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(_SKELETON_SRC).as_posix(): ast.parse(path.read_text()) for path in _SKELETON_SRC.rglob("*.py")
    }


def test_every_channel_send_in_the_skeleton_is_at_a_recorded_site():
    assert unrecorded_sends(_skeleton_modules()) == []


def test_every_recorded_site_still_sends():
    modules = _skeleton_modules()
    sites = {(path, function) for path, tree in modules.items() for _line, function in _send_calls(tree)}

    assert sites >= _RECORDED_SITES


def test_a_module_with_an_unrecorded_notify_fails_the_guard():
    source = textwrap.dedent(
        """
        async def tell(channel, notification):
            return await channel.notify(notification)
        """
    )

    assert unrecorded_sends({"conversations/elsewhere.py": ast.parse(source)}) == [
        ("conversations/elsewhere.py", 3, "tell")
    ]


def test_a_send_inside_a_recorded_function_of_another_module_fails_the_guard():
    source = textwrap.dedent(
        """
        async def _attempt_loop(channel_obj, frame):
            await channel_obj.deliver(frame)
        """
    )

    assert unrecorded_sends({"interactions/other.py": ast.parse(source)}) == [
        ("interactions/other.py", 3, "_attempt_loop")
    ]

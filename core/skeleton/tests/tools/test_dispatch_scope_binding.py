"""The door-layer binding applied at the shared run_tool/dispatch_scope CHOKEPOINT.

Proves, over the real ``run_tool`` seam with a registered preset and a faked ``states``
facet (store off, so the runs-index is a no-op):

* a DOOR binding deposited on the ambient ``ToolInvocation`` SURVIVES the dispatch_scope
  re-deposit (read-then-set carry-forward) — the running tool sees it;
* the merged binding's injection writes the run input BEFORE the dispatch and its update
  applies through the store AFTER it;
* a door binding + the preset's OWN binding for the SAME state MERGE (door subject wins,
  templates union, injections/updates concatenated door-first);
* a dispatch with NO binding is byte-for-byte the prior dispatch — the states facet is
  never touched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.states import (
    ApplyResult,
    StateAttach,
    StateBinding,
    StateInjection,
    StateRecord,
    StateUpdate,
    TemplateJqApplyResult,
    TemplateJqResult,
)
from tai42_contract.template import TemplatedText
from tai42_contract.tools import (
    ToolInvocation,
    current_tool_invocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.dispatch_scope import dispatch_scope


class _FakeStates:
    """Records the binding runtime's calls; PATCHED onto the real facet instance (only the
    methods the dispatch path uses) so app startup's own facet wiring stays intact."""

    def __init__(self, record: dict[str, Any] | None = None) -> None:
        self._record = record or {}
        self.evals: list[str] = []
        self.applies: list[tuple[str, Any]] = []

    def patch_onto(self, facet: Any) -> None:
        facet.context = self.context
        facet.read = self.read
        facet.eval_template_jq = self.eval_template_jq
        facet.apply_template_jq = self.apply_template_jq
        facet.apply = self.apply

    def context(self):
        return None

    async def read(self, state, subject):
        return StateRecord(state=state, subject=subject, data=self._record, seq=1.0, canonical_subject=subject)

    async def eval_template_jq(self, state, subject, name, params) -> TemplateJqResult:
        self.evals.append(name)
        return TemplateJqResult(name=name, value={"from": name})

    async def apply_template_jq(self, state, subject, name, input, *, op_id, origin) -> TemplateJqApplyResult:
        self.applies.append((name, input))
        return TemplateJqApplyResult(name=name, applied=True, data={}, seq=1.0, skipped=[])

    async def apply(self, state, subject, ops, *, op_id, origin) -> ApplyResult:
        self.applies.append(("__custom__", ops))
        return ApplyResult(applied=True, data={}, seq=1.0, skipped=[])


_SUBJECT_EXPR = TemplatedText(content='{target_kind: "agent", target_name: "a", kind: "thread", key: (.x | tostring)}')


def _attach(**kw: Any) -> StateAttach:
    kw.setdefault("state", "status")
    kw.setdefault("subject_expr", _SUBJECT_EXPR)
    return StateAttach(**kw)


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


async def _register_preset(name: str, binding: StateBinding | None, seen: dict[str, Any]) -> None:
    @app.tools.tool(force=True)
    async def base(x: int, injected: dict | None = None, p: dict | None = None) -> dict:
        """Probe base tool: records the injected input and the ambient binding it sees."""
        seen["injected"] = injected
        seen["p"] = p
        inv = current_tool_invocation()
        seen["binding"] = inv.state_binding if inv is not None else None
        return {"ok": x}

    await app.preset_manager.register(name, "base", {}, [], "a preset", state_binding=binding, version=1)


def test_door_and_preset_binding_merge_inject_before_and_update_after() -> None:
    seen: dict[str, Any] = {}
    fake = _FakeStates(record={"n": 1})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake.patch_onto(app._states_facet)
            preset_binding = StateBinding(
                states=[
                    _attach(
                        subject_expr=TemplatedText(content=".missing_key"),  # a DIFFERENT subject the door overrides
                        input_injections=[StateInjection(template_jq="preset_view", into="p")],
                        updates=[
                            StateUpdate(template_jq="preset_mark", adapter=TemplatedText(content="{v: .output.ok}"))
                        ],
                    )
                ]
            )
            await _register_preset("p1", preset_binding, seen)

            door_binding = StateBinding(
                states=[
                    _attach(
                        input_injections=[StateInjection(jq=TemplatedText(content="{n: .record.n}"), into="injected")],
                        updates=[
                            StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["last"], value: .output.ok}]'))
                        ],
                    )
                ]
            )
            token = set_current_tool_invocation(ToolInvocation(tool_name="p1", state_binding=door_binding))
            try:
                result = await app.tools.run_tool("p1", {"x": 5})
            finally:
                reset_current_tool_invocation(token)

            assert result == {"ok": 5}
            # Injection ran BEFORE the dispatch: the custom door injection wrote ``injected``.
            assert seen["injected"] == {"n": 1}
            # The preset's named injection also ran (merge concatenated door-first, preset second).
            assert "preset_view" in fake.evals
            # Carry-forward: the running tool sees the DOOR binding re-deposited on the ambient
            # context (dispatch_scope carries it past its own re-deposit); the MERGED binding is
            # used for the apply, never deposited. Here the door binding's own subject_expr shows.
            assert seen["binding"] is not None
            assert seen["binding"].states[0].subject_expr == _SUBJECT_EXPR
            # Update ran AFTER the dispatch: both the custom (door) and named (preset) updates.
            kinds = {name for name, _ in fake.applies}
            assert "__custom__" in kinds
            assert "preset_mark" in kinds

    asyncio.run(run())


def test_no_binding_dispatch_never_touches_the_states_facet() -> None:
    seen: dict[str, Any] = {}
    fake = _FakeStates()

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake.patch_onto(app._states_facet)
            await _register_preset("p2", None, seen)
            result = await app.tools.run_tool("p2", {"x": 9})
            assert result == {"ok": 9}
            assert seen["injected"] is None  # nothing injected
            assert seen["binding"] is None  # no binding on the ambient context
            assert fake.evals == []  # facet untouched
            assert fake.applies == []

    asyncio.run(run())


def test_door_binding_applies_to_a_PLAIN_tool_target() -> None:
    # H1: a door binding whose target is a plain tool (NOT a registered preset) still applies
    # at the outermost dispatch — injection before, update after.
    seen: dict[str, Any] = {}
    fake = _FakeStates(record={"n": 7})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake.patch_onto(app._states_facet)

            @app.tools.tool(force=True)
            async def plain(x: int, injected: dict | None = None) -> dict:
                seen["injected"] = injected
                return {"ok": x}

            binding = StateBinding(
                states=[
                    _attach(
                        input_injections=[StateInjection(jq=TemplatedText(content="{n: .record.n}"), into="injected")],
                        updates=[
                            StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["last"], value: .output.ok}]'))
                        ],
                    )
                ]
            )
            token = set_current_tool_invocation(ToolInvocation(tool_name="plain", state_binding=binding))
            try:
                result = await app.tools.run_tool("plain", {"x": 3})
            finally:
                reset_current_tool_invocation(token)
            assert result == {"ok": 3}
            assert seen["injected"] == {"n": 7}  # injection landed on a non-preset dispatch
            assert ("__custom__", [{"op": "set", "path": ["last"], "value": 3}]) in fake.applies

    asyncio.run(run())


def test_nested_preset_dispatch_does_not_apply_its_own_binding() -> None:
    # M3: a preset's own binding fires only when it is the OUTERMOST dispatch. Dispatched as a
    # sub-tool (nested), it applies nothing — no double apply.
    seen: dict[str, Any] = {}
    fake = _FakeStates(record={})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake.patch_onto(app._states_facet)
            inner_binding = StateBinding(
                states=[
                    _attach(
                        updates=[
                            StateUpdate(template_jq="inner_mark", adapter=TemplatedText(content="{v: .output.ok}"))
                        ]
                    )
                ]
            )
            await _register_preset("inner", inner_binding, seen)

            @app.tools.tool(force=True)
            async def outer(x: int) -> dict:
                # A nested dispatch of the preset from inside another tool.
                return await app.tools.run_tool("inner", {"x": x})

            # Dispatched NESTED (via outer): the preset's own binding must NOT apply.
            await app.tools.run_tool("outer", {"x": 1})
            assert fake.applies == []
            # Dispatched OUTERMOST (directly): the preset's own binding DOES apply.
            await app.tools.run_tool("inner", {"x": 2})
            assert any(name == "inner_mark" for name, _ in fake.applies)

    asyncio.run(run())


def test_in_process_park_result_applies_no_updates_but_injects_and_records_parked(monkeypatch) -> None:
    # HIGH: an async-park ``SuspendedInteraction`` returned up through the in-process
    # ``run_tool`` door is NOT a clean output. The binding's UPDATES must not apply (a park
    # writes nothing to state), while its INJECTIONS — which ran BEFORE the dispatch — stand,
    # and the run row records ``parked``. This mirrors the MCP edge's ``observe_park`` so both
    # doors share one invariant: updates run ONLY on a real output.
    seen: dict[str, Any] = {}
    fake = _FakeStates(record={"n": 4})
    terminals: list[dict[str, Any]] = []

    class _SpyRuns:
        async def insert_start(
            self, run_id, preset_name, preset_version, *, trace_id, user_id, session_id, interaction_id, started_at
        ):
            pass

        async def update_outcome(self, run_id, outcome, ended_at, *, trace_id=None, interaction_id=None):
            terminals.append({"outcome": outcome, "interaction_id": interaction_id})

    import tai42_skeleton.runs.chokepoint as chokepoint

    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: _SpyRuns())
    monkeypatch.setattr(chokepoint, "_safe_trace_id", lambda: None)

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake.patch_onto(app._states_facet)

            @app.tools.tool(force=True)
            async def parker(x: int, injected: dict | None = None) -> SuspendedInteraction:
                """Probe tool that async-parks: returns the suspended sentinel in place of an output."""
                seen["injected"] = injected
                return SuspendedInteraction(interaction_id="i-inproc", expiry_at=None)

            binding = StateBinding(
                states=[
                    _attach(
                        input_injections=[StateInjection(jq=TemplatedText(content="{n: .record.n}"), into="injected")],
                        updates=[StateUpdate(jq=TemplatedText(content='[{op: "set", path: ["last"], value: 1}]'))],
                    )
                ]
            )
            await app.preset_manager.register("pp", "parker", {}, [], "d", state_binding=binding, version=1)

            result = await app.tools.run_tool("pp", {"x": 5})
            assert isinstance(result, SuspendedInteraction)
            # Injection ran BEFORE the park — the tool saw the injected input.
            assert seen["injected"] == {"n": 4}
            # NO update applied: a park is not a clean output.
            assert fake.applies == []
            # The run row records the park by the sentinel's interaction id.
            assert terminals == [{"outcome": "parked", "interaction_id": "i-inproc"}]

    asyncio.run(run())


def test_mcp_edge_injects_into_absent_arguments_reaching_the_dispatch() -> None:
    # LOW→loss: an MCP ``tools/call`` with NO ``arguments`` (the wire's absent → None) must
    # still deliver the binding's injections TO THE DISPATCH. The edge fires
    # ``context.message`` itself, so the injection target and the dispatched arguments are ONE
    # object — a throwaway ``{}`` would drop the injection.
    from types import SimpleNamespace
    from typing import cast

    from fastmcp.server.middleware import MiddlewareContext

    from tai42_skeleton.tools.dispatch_scope import DispatchScopeMiddleware

    seen: dict[str, Any] = {}
    fake = _FakeStates(record={"n": 3})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake.patch_onto(app._states_facet)
            binding = StateBinding(
                states=[
                    _attach(
                        input_injections=[StateInjection(jq=TemplatedText(content="{n: .record.n}"), into="injected")]
                    )
                ]
            )
            await _register_preset("pm", binding, seen)

            mw = DispatchScopeMiddleware(app)
            message = SimpleNamespace(name="pm", arguments=None)
            context = SimpleNamespace(message=message)
            dispatched: dict[str, Any] = {}

            async def call_next(_ctx: object) -> object:
                # The edge fires ``context.message`` — read the arguments it will dispatch.
                dispatched["arguments"] = message.arguments
                return SimpleNamespace(structured_content=None)

            await mw.on_call_tool(cast(MiddlewareContext[Any], context), call_next)

            # Absent arguments were normalized to ONE real dict, injected into AND dispatched
            # with — the edge fired the injected input, never a discarded throwaway.
            assert dispatched["arguments"] is not None
            assert dispatched["arguments"]["injected"] == {"n": 3}

    asyncio.run(run())


def test_injections_run_even_when_arguments_is_None_one_invariant() -> None:
    # A dispatch that carried NO arguments (the MCP wire's ``None``) must not silently SKIP
    # the binding's injections while its updates still fire — injections and updates share ONE
    # guard, both engaged over ``{}`` when the door carried none.
    seen: dict[str, Any] = {}
    fake = _FakeStates(record={"n": 2})

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            fake.patch_onto(app._states_facet)
            binding = StateBinding(
                states=[
                    _attach(
                        input_injections=[StateInjection(template_jq="v_in", into="p")],
                        updates=[StateUpdate(template_jq="v_up", adapter=TemplatedText(content="{v: 1}"))],
                    )
                ]
            )
            await _register_preset("pn", binding, seen)
            async with dispatch_scope(app, "pn", None) as scope:
                scope.observe({"ok": True})
            # Injection engaged despite ``arguments is None`` (no silent skip)...
            assert "v_in" in fake.evals
            # ...and the update engaged on the clean success — one shared guard.
            assert any(name == "v_up" for name, _ in fake.applies)

    asyncio.run(run())

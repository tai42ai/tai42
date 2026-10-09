"""The rendered-template facet reads and the rendered-template cache.

A neutral synthetic consumer — a template named ``probe`` with inline and by-id program bodies —
reads its rendered template through ``get_rendered_template`` / ``rendered_attachments`` /
``render_template`` against the real :class:`StatesService` over the in-memory Postgres. The cache
keys on the template version and the rendering resource manager's epoch and eviction generation:
a row write, each local eviction seam, a rebuilt manager and an expired TTL each cause exactly one
re-render, and only a successful render is kept.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import StateNotFoundError, TemplateValidationError
from tai42_contract.states.models import (
    AttachBody,
    StateDeclaration,
    StateSubject,
    StateTemplateDocument,
    StateTemplateParameter,
    StateTemplateTrace,
    WriteOrigin,
)
from tai42_contract.states.rendered import (
    RenderedAttachment,
    RenderedStateTemplate,
    RenderedTemplateDeclarations,
    RenderedTemplateJq,
)
from tai42_contract.template import TemplatedText

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.service.rendered import rendered_digest
from tai42_skeleton.states.store import PostgresStatesStore
from tai42_skeleton.template.resource_manager import ResourceManager, TemplateNotFoundError
from tai42_skeleton.template.settings import TemplateCacheSettings

from ..app._fixtures.reload import reload_with
from .conftest import FakeStatesPg

_ORIGIN = WriteOrigin(consumer="probe")
_SUBJECT = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")

_STORED = {"stored-count": "(.items // []) | length", "stored-check": "true"}


class _StubManager:
    """The resource-manager surface the states service renders through, counting every render."""

    def __init__(self, *, epoch: int = 1, cache_enabled: bool = True) -> None:
        self.epoch = epoch
        self.generation = 0
        self.cache_enabled = cache_enabled
        self.renders: list[str] = []

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        self.renders.append(text.id or text.content or "")
        if text.id is not None:
            if text.id not in _STORED:
                raise TemplateNotFoundError(f"no stored resource {text.id!r}")
            return _STORED[text.id]
        assert text.content is not None
        return text.content


class _App:
    def __init__(self, manager: Any) -> None:
        self.storage = type("S", (), {"resource_manager": manager})()


_PROBE = {
    "kind": "state-template",
    "name": "probe",
    "parameters": {"cap": {"schema": {"type": "integer"}, "default": 3}},
    "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
    "regimes": [{"path": ["items"], "regime": "composing"}],
    "declarations": {"schema": {"type": "object"}, "check": {"id": "stored-check"}},
    "template_jq": {
        "total": {"purpose": "input", "jq": {"content": "tjq_count({}) + $parameters.cap"}},
        "count": {"purpose": "input", "jq": {"id": "stored-count"}},
        "add": {
            "purpose": "update",
            "params": ["v"],
            "reads": [["items"]],
            "writes": [["items"]],
            "jq": {"content": '[{"op": "set", "path": ["items", "-"], "value": $input.v}]'},
        },
    },
}

_DECL = StateDeclaration(
    name="alerts",
    schema={"type": "object", "properties": {"n": {}}},
    subject_kinds=["thread"],
    default_subject_kind="thread",
)


@pytest.fixture
def manager() -> _StubManager:
    return _StubManager()


@pytest.fixture
def svc(pg: FakeStatesPg, manager: _StubManager, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_App(manager)):  # type: ignore[arg-type]
        yield StatesService(store=PostgresStatesStore())


async def _setup(svc: StatesService) -> None:
    await svc.put_declaration(_DECL)
    await svc.put_template(StateTemplateDocument.model_validate(_PROBE), replace=False)
    await svc.attach("alerts", "probe", AttachBody(path=["p"], declarations={}))


def _expected_template(version: str) -> RenderedStateTemplate:
    return RenderedStateTemplate(
        name="probe",
        description="",
        version=version,
        parameters={"cap": StateTemplateParameter.model_validate({"schema": {"type": "integer"}, "default": 3})},
        schema={"type": "object", "properties": {"items": {"type": "array"}}},
        regimes=_PROBE_REGIMES,
        declarations=RenderedTemplateDeclarations(schema={"type": "object"}, check="true"),
        trace=StateTemplateTrace(),
        template_jq={
            "total": RenderedTemplateJq(purpose="input", jq="tjq_count({}) + $parameters.cap", params=[]),
            "count": RenderedTemplateJq(purpose="input", jq="(.items // []) | length", params=[]),
            "add": RenderedTemplateJq(
                purpose="update",
                jq='[{"op": "set", "path": ["items", "-"], "value": $input.v}]',
                params=["v"],
                reads=[["items"]],
                writes=[["items"]],
            ),
        },
        input_order=["count", "total"],
    )


_PROBE_REGIMES = StateTemplateDocument.model_validate(_PROBE).regimes


def _token(prefix: str) -> str:
    """The served token of the ``probe`` render at ``prefix`` (``"<version>:<epoch>.<generation>"``)."""
    return f"{prefix}:{rendered_digest(_expected_template('candidate'))}"


async def test_a_consumer_reads_the_rendered_attachments(svc: StatesService, pg: FakeStatesPg) -> None:
    await _setup(svc)
    rendered = await svc.rendered_attachments("alerts")
    decl_version = pg.declarations["alerts"]["version"]
    template_version = pg.templates["probe"]["version"]
    assert rendered == [
        RenderedAttachment(
            state="alerts",
            template=_expected_template(_token(f"{template_version}:1.0")),
            path=["p"],
            parameters={"cap": 3},
            declarations={},
            version=f"{decl_version}:{_token(f'{template_version}:1.0')}",
        )
    ]


async def test_get_rendered_template_and_absent(svc: StatesService, pg: FakeStatesPg) -> None:
    await _setup(svc)
    expected = _expected_template(_token(f"{pg.templates['probe']['version']}:1.0"))
    assert await svc.get_rendered_template("probe") == expected
    assert await svc.get_rendered_template("absent") is None


async def test_rendered_attachments_refuses_an_undeclared_state(svc: StatesService) -> None:
    with pytest.raises(StateNotFoundError, match="no state declared"):
        await svc.rendered_attachments("absent")


async def test_render_template_renders_a_candidate_uncached(svc: StatesService, manager: _StubManager) -> None:
    doc = StateTemplateDocument.model_validate(_PROBE)
    first = await svc.render_template(doc)
    assert first == _expected_template("candidate")
    renders = len(manager.renders)
    await svc.render_template(doc)
    assert len(manager.renders) > renders  # a candidate is never served from the cache


async def test_render_template_refuses_an_invalid_candidate(svc: StatesService) -> None:
    bad = StateTemplateDocument.model_validate(
        {**_PROBE, "template_jq": {"x": {"purpose": "input", "jq": {"content": "("}}}}
    )
    with pytest.raises(TemplateValidationError):
        await svc.render_template(bad)


async def _evaluate(svc: StatesService, n: int) -> None:
    for _ in range(n):
        result = await svc.eval_template_jq("alerts", _SUBJECT, "total", {})
        assert result.value == 3


async def test_n_evaluations_render_each_body_once(svc: StatesService, manager: _StubManager) -> None:
    await _setup(svc)
    manager.renders.clear()
    await _evaluate(svc, 5)
    # One render of the three program bodies and the check, then every evaluation is a hit.
    assert sorted(manager.renders) == sorted(
        [
            "tjq_count({}) + $parameters.cap",
            "stored-count",
            _PROBE["template_jq"]["add"]["jq"]["content"],
            "stored-check",
        ]
    )


async def test_the_input_order_is_computed_once_per_version(
    svc: StatesService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.states.service import templates as templates_mod

    calls: list[int] = []
    real = templates_mod.input_order
    monkeypatch.setattr(templates_mod, "input_order", lambda inputs: calls.append(1) or real(inputs))
    await _setup(svc)
    await _evaluate(svc, 3)
    await svc.rendered_attachments("alerts")
    await svc.get_rendered_template("probe")
    assert len(calls) == 1


async def test_a_row_write_rerenders_once(svc: StatesService, manager: _StubManager) -> None:
    await _setup(svc)
    await _evaluate(svc, 2)
    manager.renders.clear()
    await svc.put_template(StateTemplateDocument.model_validate({**_PROBE, "description": "v2"}), replace=True)
    manager.renders.clear()  # the save renders its by-id bodies to compile them
    await _evaluate(svc, 3)
    assert len(manager.renders) == 4  # one fresh render of every body and the check


async def test_a_generation_bump_rerenders_once_and_changes_the_token(
    svc: StatesService, manager: _StubManager
) -> None:
    await _setup(svc)
    before = (await svc.rendered_attachments("alerts"))[0].version
    await _evaluate(svc, 1)
    manager.renders.clear()
    manager.generation += 1
    await _evaluate(svc, 3)
    assert len(manager.renders) == 4
    assert (await svc.rendered_attachments("alerts"))[0].version != before


async def test_a_disabled_manager_cache_disables_this_one(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    manager = _StubManager(cache_enabled=False)
    with tai42_app.bound(_App(manager)):  # type: ignore[arg-type]
        svc = StatesService(store=PostgresStatesStore())
        await _setup(svc)
        manager.renders.clear()
        await _evaluate(svc, 2)
        assert len(manager.renders) == 8
        assert len(svc._rendered_cache) == 0


async def test_an_expired_ttl_rerenders(
    svc: StatesService, manager: _StubManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.states.service import rendered as rendered_mod
    from tai42_skeleton.states.service import templates as templates_mod

    monkeypatch.setattr(templates_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=60))
    clock = [1000.0]
    monkeypatch.setattr(rendered_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(templates_mod.time, "monotonic", lambda: clock[0])
    await _setup(svc)
    await _evaluate(svc, 1)
    manager.renders.clear()
    clock[0] += 59
    await _evaluate(svc, 1)
    assert manager.renders == []
    clock[0] += 2
    await _evaluate(svc, 1)
    assert len(manager.renders) == 4


@pytest.mark.parametrize("cache_enabled", [True, False])
async def test_a_rerender_with_new_text_changes_the_token(
    pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch, cache_enabled: bool
) -> None:
    from tai42_skeleton.states.service import rendered as rendered_mod
    from tai42_skeleton.states.service import templates as templates_mod

    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    monkeypatch.setattr(templates_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=60))
    clock = [1000.0]
    monkeypatch.setattr(rendered_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(templates_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setitem(_STORED, "stored-count", "1")
    manager = _StubManager(cache_enabled=cache_enabled)
    with tai42_app.bound(_App(manager)):  # type: ignore[arg-type]
        svc = StatesService(store=PostgresStatesStore())
        await _setup(svc)
        first = (await svc.rendered_attachments("alerts"))[0]
        clock[0] += 61
        unchanged = (await svc.rendered_attachments("alerts"))[0]
        # A re-render that produces the same text keeps the token.
        assert unchanged.version == first.version
        # The stored resource changes with no eviction reaching this process; the TTL passes.
        _STORED["stored-count"] = "2"
        clock[0] += 61
        second = (await svc.rendered_attachments("alerts"))[0]
        rendered = await svc.get_rendered_template("probe")
    assert first.template.template_jq["count"].jq == "1"
    assert second.template.template_jq["count"].jq == "2"
    assert second.version != first.version
    assert second.template.version != first.template.version
    assert rendered is not None
    assert rendered.version == second.template.version


async def test_no_ttl_never_expires_by_age(
    svc: StatesService, manager: _StubManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.states.service import rendered as rendered_mod
    from tai42_skeleton.states.service import templates as templates_mod

    monkeypatch.setattr(templates_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=None))
    clock = [1000.0]
    monkeypatch.setattr(rendered_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(templates_mod.time, "monotonic", lambda: clock[0])
    await _setup(svc)
    await _evaluate(svc, 1)
    manager.renders.clear()
    clock[0] += 10**9
    await _evaluate(svc, 1)
    assert manager.renders == []


async def test_a_failed_render_is_not_cached_and_the_next_call_renders_again(
    svc: StatesService, manager: _StubManager
) -> None:
    await _setup(svc)
    _STORED.pop("stored-count")
    try:
        with pytest.raises(TemplateValidationError, match="stored-count"):
            await svc.eval_template_jq("alerts", _SUBJECT, "total", {})
        assert len(svc._rendered_cache) == 0
    finally:
        _STORED["stored-count"] = "(.items // []) | length"
    manager.renders.clear()
    await _evaluate(svc, 1)
    assert len(manager.renders) == 4


async def test_a_real_manager_eviction_seam_each_rerender_once(
    pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    manager = ResourceManager(None, on_evicted=lambda _eviction: None)
    renders: list[str] = []
    real_render = ResourceManager.render_templated_text

    async def counting(self: ResourceManager, text: TemplatedText, locale: str | None = None) -> str:
        renders.append(text.content or "")
        return await real_render(self, text, locale)

    monkeypatch.setattr(ResourceManager, "render_templated_text", counting)
    inline_probe = {**_PROBE, "declarations": {"schema": {"type": "object"}}}
    inline_probe["template_jq"] = {**_PROBE["template_jq"], "count": {"purpose": "input", "jq": {"content": "0"}}}
    with tai42_app.bound(_App(manager)):  # type: ignore[arg-type]
        svc = StatesService(store=PostgresStatesStore())
        await svc.put_declaration(_DECL)
        await svc.put_template(StateTemplateDocument.model_validate(inline_probe), replace=False)
        await svc.attach("alerts", "probe", AttachBody(path=["p"]))
        await _evaluate(svc, 1)
        for evict in (lambda: manager.evict_compiled("x"), lambda: manager.evict_dir("d"), manager.clear_cache):
            renders.clear()
            evict()
            await _evaluate(svc, 2)
            assert len(renders) == 3  # every program body once


def test_a_reload_rerenders_through_the_rebuilt_manager(pg: FakeStatesPg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    renders: list[int] = []
    real_render = ResourceManager.render_templated_text

    async def counting(self: ResourceManager, text: TemplatedText, locale: str | None = None) -> str:
        renders.append(self.epoch)
        return await real_render(self, text, locale)

    monkeypatch.setattr(ResourceManager, "render_templated_text", counting)
    inline_probe = {**_PROBE, "declarations": {"schema": {"type": "object"}}}
    inline_probe["template_jq"] = {**_PROBE["template_jq"], "count": {"purpose": "input", "jq": {"content": "0"}}}
    manifest = Manifest.model_validate({})

    async def run() -> None:
        async with app.app_context(manifest):
            svc = StatesService(store=PostgresStatesStore())
            await svc.put_declaration(_DECL)
            await svc.put_template(StateTemplateDocument.model_validate(inline_probe), replace=False)
            await svc.attach("alerts", "probe", AttachBody(path=["p"]))
            await _evaluate(svc, 1)
            manager_a = app.storage.resource_manager
            before = (await svc.rendered_attachments("alerts"))[0].version
            renders.clear()
            await reload_with(app, manifest)
            await _evaluate(svc, 2)
            manager_b = app.storage.resource_manager
            assert manager_b is not manager_a
            assert manager_b.epoch != manager_a.epoch
            assert renders == [manager_b.epoch] * 3  # one more render of each body, through B
            assert (await svc.rendered_attachments("alerts"))[0].version != before

    asyncio.run(run())


def test_two_managers_built_in_sequence_carry_different_epochs() -> None:
    first, second = (
        ResourceManager(None, on_evicted=lambda _eviction: None),
        ResourceManager(None, on_evicted=lambda _eviction: None),
    )
    assert second.epoch > first.epoch

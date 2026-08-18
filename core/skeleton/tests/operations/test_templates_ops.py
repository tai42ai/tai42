"""Op-level oracles for the templates operations.

``upload_template`` takes a ``path`` argument and returns ``{"path", "uploaded", "fanout"}``;
the traversal guard raises the typed ``BadRequestError`` (the route's ``400``). The
other ops are pinned directly (the route oracles pin the same behavior through the
adapter). Projection carries ``destructiveHint`` for the mutating ops.

The compiled template is held in a per-worker cache, so every write op writes the store,
then broadcasts an ``evict_template`` op (``clear_templates_cache`` broadcasts
``clear_template_cache``) on the worker bus so the whole fleet drops the stale
compilation; the response embeds the per-worker fleet report. These oracles pin that
broadcast (op shape + self-entry payload) against the recording :class:`FakeBus`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.manifest import ApiToolsConfig

import tai42_skeleton.operations.templates as templates_ops
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.operations import BadRequestError, NotFoundError, OperationRegistry, operation_metadata_of
from tai42_skeleton.operations._broadcast import FleetBroadcastError
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.operations.templates import (
    clear_templates_cache,
    delete_template,
    delete_template_dir,
    get_template,
    list_templates,
    render_template,
    upload_template,
)
from tai42_skeleton.template import TemplateNotFoundError
from tests._fakes.bus import FakeBus


class _ResourceManager:
    def __init__(self, *, listed: list[str] | None = None) -> None:
        self.uploaded: dict[str, str] = {}
        self.deleted: list[str] = []
        self.cleared = False
        self._listed = listed or []

    async def list_resources(self) -> list[str]:
        return self._listed

    async def fetch_template(self, template_id: str) -> str:
        return f"content of {template_id}"

    async def get_template_schema(self, content=None, template_id=None) -> dict:
        return {"vars": ["name"]}

    async def upload_template(self, path: str, content: str) -> None:
        self.uploaded[path] = content

    async def delete_template(self, path: str) -> None:
        self.deleted.append(path)

    async def delete_template_dir(self, path: str) -> None:
        self.deleted.append(path)

    async def render_by_id_or_content(self, content=None, template_id=None, kwargs=None) -> str:
        return f"rendered:{template_id or content}:{kwargs}"

    def clear_cache(self) -> None:
        self.cleared = True


def _install(monkeypatch: pytest.MonkeyPatch, tm: _ResourceManager, bus: FakeBus | None = None) -> FakeBus:
    """Point the op module's ``tai42_app`` at ``tm`` and install a recording bus so the
    fleet ``evict_template`` / ``clear_template_cache`` broadcast is assertable."""
    fake_app = SimpleNamespace(storage=SimpleNamespace(resource_manager=tm))
    monkeypatch.setattr(templates_ops, "tai42_app", fake_app)
    bus = bus or FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> _ResourceManager:
    tm = _ResourceManager()
    _install(monkeypatch, tm)
    return tm


# -- upload_template --


async def test_upload_template_delegates_and_returns_path_uploaded(manager: _ResourceManager) -> None:
    # Arg ``path``; returns ``{"path", "uploaded", "fanout"}``.
    result = await upload_template("greeting.j2", "Hi {{ name }}")

    assert result["path"] == "greeting.j2"
    assert result["uploaded"] is True
    assert result["fanout"]["mode"] == "local-only"  # no sibling bus configured
    assert manager.uploaded == {"greeting.j2": "Hi {{ name }}"}


async def test_upload_template_writes_store_then_broadcasts_evict(monkeypatch: pytest.MonkeyPatch) -> None:
    # The store write is this worker's local apply, and the op broadcasts an untargeted
    # ``evict_template`` carrying the key so every sibling drops its stale compilation.
    # Removing the broadcast (writing the store directly) leaves this unasserted — the
    # exact per-worker liveness bug this op closes.
    tm = _ResourceManager()
    bus = _install(monkeypatch, tm, FakeBus(remotes=["serve-w1"]))

    result = await upload_template("greeting.j2", "Hi {{ name }}")

    assert tm.uploaded == {"greeting.j2": "Hi {{ name }}"}
    assert bus.publish_calls == [
        (
            {"op": "evict_template", "path": "greeting.j2"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=None),
        )
    ]
    # The response embeds the per-worker fleet report so a caller has deterministic proof
    # the eviction propagated.
    assert result["fanout"]["mode"] == "fleet"
    assert {r["name"]: r["outcome"] for r in result["fanout"]["results"]} == {
        "serve-test": "applied",
        "serve-w1": "applied",
    }


@pytest.mark.parametrize("bad", ["/abs.j2", "../escape.j2", "a/../../etc", "back\\slash"])
async def test_upload_template_rejects_traversal_path(manager: _ResourceManager, bad: str) -> None:
    # A traversal path raises the route's typed ``BadRequestError`` (400) — never
    # reaching the store.
    with pytest.raises(BadRequestError):
        await upload_template(bad, "content")
    assert manager.uploaded == {}


async def test_upload_template_rejects_non_string_content(manager: _ResourceManager) -> None:
    with pytest.raises(BadRequestError, match="content must be a string"):
        await upload_template("ok.j2", 123)  # type: ignore[arg-type]
    assert manager.uploaded == {}


# -- get / delete / render / list / clear characterization ---------------------


async def test_get_template_returns_content_and_schema(manager: _ResourceManager) -> None:
    result = await get_template("a.j2")
    assert result == {"template": "content of a.j2", "schema": {"vars": ["name"]}}


async def test_get_template_empty_id_is_field_specific_400(manager: _ResourceManager) -> None:
    # A blank id is a field-specific 400 naming ``template_id`` (ahead of the path
    # guard, whose generic message names ``path``).
    with pytest.raises(BadRequestError, match="template_id must be a non-empty string"):
        await get_template("")


async def test_get_template_missing_is_not_found(manager: _ResourceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _missing(template_id: str) -> str:
        raise TemplateNotFoundError(f"Template '{template_id}' not found.")

    monkeypatch.setattr(manager, "fetch_template", _missing)
    with pytest.raises(NotFoundError, match="not found"):
        await get_template("gone.j2")


async def test_delete_template_delegates(manager: _ResourceManager) -> None:
    result = await delete_template("x.j2")
    assert result["path"] == "x.j2"
    assert result["deleted"] is True
    assert result["fanout"]["mode"] == "local-only"
    assert manager.deleted == ["x.j2"]


async def test_delete_template_broadcasts_evict(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = _ResourceManager()
    bus = _install(monkeypatch, tm, FakeBus(remotes=["serve-w1"]))

    result = await delete_template("x.j2")

    assert tm.deleted == ["x.j2"]
    assert bus.publish_calls == [
        (
            {"op": "evict_template", "path": "x.j2"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=None),
        )
    ]
    assert result["fanout"]["mode"] == "fleet"


@pytest.mark.parametrize("bad", ["/abs.j2", "../escape.j2", "back\\slash"])
async def test_delete_template_rejects_traversal(manager: _ResourceManager, bad: str) -> None:
    with pytest.raises(BadRequestError):
        await delete_template(bad)
    assert manager.deleted == []


async def test_delete_template_dir_delegates(manager: _ResourceManager) -> None:
    result = await delete_template_dir("prompts/archive")
    assert result["path"] == "prompts/archive"
    assert result["deleted"] is True
    assert result["fanout"]["mode"] == "local-only"
    assert manager.deleted == ["prompts/archive"]


async def test_delete_template_dir_broadcasts_evict_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dir delete broadcasts ``evict_template`` with ``prefix`` set, so a receiving
    # worker evicts every compilation under the directory key rather than one exact key.
    tm = _ResourceManager()
    bus = _install(monkeypatch, tm, FakeBus(remotes=["serve-w1"]))

    result = await delete_template_dir("prompts/archive")

    assert tm.deleted == ["prompts/archive"]
    assert bus.publish_calls == [
        (
            {"op": "evict_template", "path": "prompts/archive", "prefix": True},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=None),
        )
    ]
    assert result["fanout"]["mode"] == "fleet"


@pytest.mark.parametrize("bad", ["/abs", "../escape", "a/../../etc", "back\\slash"])
async def test_delete_template_dir_rejects_traversal(manager: _ResourceManager, bad: str) -> None:
    with pytest.raises(BadRequestError):
        await delete_template_dir(bad)
    assert manager.deleted == []


async def test_delete_template_dir_absent_is_not_found(
    manager: _ResourceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A directory that matches nothing is a loud 404 — the provider raises
    # ``FileNotFoundError`` and the op maps it to ``NotFoundError``, never a no-op.
    async def _absent(path: str) -> None:
        raise FileNotFoundError(f"Storage directory not found: {path}")

    monkeypatch.setattr(manager, "delete_template_dir", _absent)
    with pytest.raises(NotFoundError, match="not found"):
        await delete_template_dir("prompts/gone")


@pytest.mark.parametrize("root_key", [".", "a/.."])
async def test_delete_template_dir_root_resolving_is_bad_request(manager: _ResourceManager, root_key: str) -> None:
    # Defense in depth: a key that clears the traversal guard but resolves to EXACTLY
    # the template root (wiping the whole store) is refused at the OPERATION layer
    # with a 400, BEFORE the provider is reached — never relying on a backend's own
    # root check.
    with pytest.raises(BadRequestError, match="template root"):
        await delete_template_dir(root_key)
    assert manager.deleted == []


async def test_delete_template_dir_provider_value_error_is_bad_request(
    manager: _ResourceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-root key still reaches the provider; a provider-reported boundary
    # violation (``ValueError``) is a client 400, never a 500.
    async def _boundary(path: str) -> None:
        raise ValueError("Refusing to delete the storage root; a directory path is required.")

    monkeypatch.setattr(manager, "delete_template_dir", _boundary)
    with pytest.raises(BadRequestError, match="storage root"):
        await delete_template_dir("prompts/archive")


async def test_delete_template_dir_infra_error_propagates(
    manager: _ResourceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A genuine infra failure is neither a 404 nor a 400: it propagates (a 500),
    # never masked as a client error.
    async def _boom(path: str) -> None:
        raise RuntimeError("storage down")

    monkeypatch.setattr(manager, "delete_template_dir", _boom)
    with pytest.raises(RuntimeError, match="storage down"):
        await delete_template_dir("prompts/archive")


async def test_delete_template_dir_partial_failure_still_broadcasts_evict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A mid-delete provider failure can leave the store partially mutated: some keys
    # under the prefix are already gone. The eviction MUST still fan out to siblings
    # (idempotent — a spurious evict costs one re-render) and the original error still
    # propagates loudly, wrapped in a ``FleetBroadcastError`` carrying the fleet report.
    # A pre-mutation failure (404/400) would abort before publish; this OSError is
    # neither, so it is the destructive-phase case.
    tm = _ResourceManager()
    bus = _install(monkeypatch, tm, FakeBus(remotes=["serve-w1"]))

    async def _partial(path: str) -> None:
        raise OSError("disk vanished mid-delete")

    monkeypatch.setattr(tm, "delete_template_dir", _partial)

    with pytest.raises(FleetBroadcastError, match="disk vanished mid-delete") as excinfo:
        await delete_template_dir("prompts/archive")

    # The original failure is preserved as the cause and the fleet report is attached.
    assert isinstance(excinfo.value.__cause__, OSError)
    assert excinfo.value.report.op == "evict_template"

    # The eviction fanned out despite the partial delete — the exact sibling-staleness
    # this split closes. The self entry carries the failed local apply.
    assert len(bus.publish_calls) == 1
    op, targets, local = bus.publish_calls[0]
    assert op == {"op": "evict_template", "path": "prompts/archive", "prefix": True}
    assert targets is None
    assert local is not None
    assert local.outcome == OpOutcome.failed


async def test_render_template_requires_a_source(manager: _ResourceManager) -> None:
    with pytest.raises(BadRequestError, match="one of"):
        await render_template()


async def test_render_template_rejects_both(manager: _ResourceManager) -> None:
    with pytest.raises(BadRequestError, match="not both"):
        await render_template(content="hi", template_id="a.j2")


async def test_render_template_by_id(manager: _ResourceManager) -> None:
    result = await render_template(template_id="a.j2", kwargs={"name": "Z"})
    assert "rendered:a.j2" in result["rendered"]


async def test_render_template_rejects_non_dict_kwargs(manager: _ResourceManager) -> None:
    with pytest.raises(BadRequestError, match="'kwargs' must be a JSON object"):
        await render_template(template_id="a.j2", kwargs=["not", "a", "dict"])  # type: ignore[arg-type]


async def test_render_template_rejects_non_string_content(manager: _ResourceManager) -> None:
    with pytest.raises(BadRequestError, match="'content' must be a string"):
        await render_template(content=123)  # type: ignore[arg-type]


async def test_render_template_missing_is_not_found(manager: _ResourceManager, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _missing(content=None, template_id=None, kwargs=None) -> str:
        raise TemplateNotFoundError("Template 'gone.j2' not found.")

    monkeypatch.setattr(manager, "render_by_id_or_content", _missing)
    with pytest.raises(NotFoundError):
        await render_template(template_id="gone.j2")


async def test_list_templates_returns_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = _ResourceManager(listed=["a.j2", "b.j2"])
    monkeypatch.setattr(templates_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=tm)))
    assert await list_templates() == ["a.j2", "b.j2"]


async def test_clear_templates_cache(manager: _ResourceManager) -> None:
    result = await clear_templates_cache()
    assert result["cleared"] is True
    assert result["fanout"]["mode"] == "local-only"
    assert manager.cleared is True


async def test_clear_templates_cache_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manual escape hatch broadcasts ``clear_template_cache`` so every worker
    # cold-starts its compiled cache, not only the one that served the call.
    tm = _ResourceManager()
    bus = _install(monkeypatch, tm, FakeBus(remotes=["serve-w1"]))

    result = await clear_templates_cache()

    assert tm.cleared is True
    assert bus.publish_calls == [
        (
            {"op": "clear_template_cache"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=None),
        )
    ]
    assert result["fanout"]["mode"] == "fleet"


# -- projection: the mutating ops carry destructiveHint ------------------------


def test_upload_and_delete_project_with_destructive_hint() -> None:
    reg = OperationRegistry()
    for op in (upload_template, delete_template, delete_template_dir, get_template, list_templates):
        reg.register(operation_metadata_of(op))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, dict] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = {"annotations": annotations}
            return lambda fn: fn

    app = SimpleNamespace(tools=_Rec())
    names = project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)

    assert {"upload_template", "delete_template", "delete_template_dir", "get_template", "list_templates"} <= set(names)
    assert app.tools.registered["upload_template"]["annotations"].destructiveHint is True
    assert app.tools.registered["delete_template"]["annotations"].destructiveHint is True
    assert app.tools.registered["delete_template_dir"]["annotations"].destructiveHint is True
    assert app.tools.registered["get_template"]["annotations"] is None  # read, not destructive

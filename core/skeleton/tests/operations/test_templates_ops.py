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
from pydantic import ValidationError
from tai42_contract.manifest import ApiToolsConfig
from tai42_contract.template import TemplatedText

import tai42_skeleton.operations.templates as templates_ops
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.conversations.media_meta import declare_media_prefix_marks
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    OperationFailedError,
    OperationRegistry,
    operation_metadata_of,
)
from tai42_skeleton.operations._broadcast import FleetBroadcastError
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.operations.response_models_group_b import TemplateTextEntry
from tai42_skeleton.operations.templates import (
    clear_templates_cache,
    delete_template,
    delete_template_dir,
    get_template,
    list_templates,
    render_template,
    upload_template,
)
from tai42_skeleton.template import ResourceManager, TemplateNotFoundError

from .._fakes.bus import FakeBus
from .._fakes.content_fixtures import (
    FILES_INSTALL_LINE,
    LOCKED_PDF_MESSAGE,
    NOTES_PDF,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    locked_pdf,
    png,
)
from .._fakes.prefix_marks import fresh_prefix_marks
from .._fakes.recording_storage import RecordingStorage
from .._fakes.template_holders import (
    HELD_DIRECTORY_REFUSAL,
    HELD_HOLDERS,
    HELD_TEMPLATE_REFUSAL,
    STORED_PRESET_FAULT,
    fresh_referees,
    hold_jq_gate,
    hook_referee,
    stored_preset_fault_referee,
    value_error_referee,
)


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

    async def render_templated_text(self, text, locale=None) -> str:
        return f"rendered:{text.id or text.content}:{text.kwargs}"

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


@pytest.fixture
def entry_store(monkeypatch: pytest.MonkeyPatch) -> RecordingStorage:
    """A recording provider behind the real resource manager, read by ``get_template``."""
    provider = RecordingStorage()
    monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
    tm = ResourceManager(provider, on_evicted=lambda _eviction: None)
    monkeypatch.setattr(templates_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=tm)))
    return provider


_ANY_SCALAR = [{"type": "boolean"}, {"type": "null"}, {"type": "number"}, {"type": "string"}]


async def test_get_template_text_entry_carries_the_inferred_schema(entry_store: RecordingStorage) -> None:
    entry_store.put("greet.j2", b"Hello {{ name }}", "text/plain")
    assert await get_template("greet.j2") == {
        "kind": "text",
        "id": "greet.j2",
        "content_type": "text/plain",
        "size": 16,
        "content": "Hello {{ name }}",
        "schema": {
            "type": "object",
            "properties": {"name": {"title": "name", "anyOf": _ANY_SCALAR}},
            "required": ["name"],
        },
        "schema_error": None,
    }


async def test_get_template_marks_a_schema_the_inference_cannot_type(entry_store: RecordingStorage) -> None:
    entry_store.put("d.j2", b"{{ {'a': 1} }}{{ who }}")
    entry = await get_template("d.j2")
    assert entry["schema"]["x-tai42-inference"] == "partial"
    assert entry["schema_error"] is None


async def test_get_template_serves_broken_jinja_with_the_schema_error(entry_store: RecordingStorage) -> None:
    entry_store.put("broken.j2", b"Hello {{ name }")
    entry = await get_template("broken.j2")
    assert entry["content"] == "Hello {{ name }"
    assert entry["schema"] is None
    assert entry["schema_error"] == "template error: unexpected '}'"


async def test_get_template_document_entry_has_a_null_schema_and_no_error(entry_store: RecordingStorage) -> None:
    entry_store.put("docs/notes.pdf", NOTES_PDF, "application/pdf")
    entry = await get_template("docs/notes.pdf")
    assert entry["kind"] == "document"
    assert "Quarterly notes" in entry["content"]
    assert entry["schema"] is None
    assert "schema_error" not in entry


async def test_get_template_binary_entry_has_a_null_schema(entry_store: RecordingStorage) -> None:
    image = png()
    entry_store.put("img/a.png", image, "image/png")
    assert await get_template("img/a.png") == {
        "kind": "binary",
        "id": "img/a.png",
        "content_type": "image/png",
        "size": len(image),
        "schema": None,
    }


async def test_get_template_reads_an_unlisted_key(entry_store: RecordingStorage, monkeypatch) -> None:
    fresh_prefix_marks(monkeypatch)
    declare_media_prefix_marks()
    entry_store.put("inbound-media/x", png(), "image/png")
    assert (await get_template("inbound-media/x"))["kind"] == "binary"


async def test_get_template_empty_id_is_field_specific_400(entry_store: RecordingStorage) -> None:
    # A blank id is a field-specific 400 naming ``template_id`` (ahead of the key
    # guard, whose message names the key).
    with pytest.raises(BadRequestError, match="template_id must be a non-empty string"):
        await get_template("")


async def test_get_template_missing_is_not_found(entry_store: RecordingStorage) -> None:
    with pytest.raises(NotFoundError, match=r"^resource 'gone\.j2' not found$"):
        await get_template("gone.j2")


@pytest.mark.parametrize("key", ["a//b", "../x"])
async def test_get_template_refuses_a_non_canonical_key(entry_store: RecordingStorage, key: str) -> None:
    with pytest.raises(BadRequestError) as caught:
        await get_template(key)
    assert caught.value.message == _non_canonical(key)
    assert entry_store.calls == []


async def test_get_template_without_a_provider_is_the_501(entry_store: RecordingStorage, monkeypatch) -> None:
    monkeypatch.setattr(instance.app._storage_registry, "_provider", None)
    with pytest.raises(NotSupportedError):
        await get_template("a.j2")


async def test_get_template_refuses_an_extractors_failure_with_its_text(entry_store: RecordingStorage) -> None:
    entry_store.put("docs/locked.pdf", locked_pdf())
    entry_store.put("data/scores.csv", RAGGED_CSV)
    with pytest.raises(BadRequestError) as locked:
        await get_template("docs/locked.pdf")
    assert locked.value.message == f"resource 'docs/locked.pdf' cannot be loaded: {LOCKED_PDF_MESSAGE}"
    with pytest.raises(BadRequestError) as ragged:
        await get_template("data/scores.csv")
    assert ragged.value.message == f"resource 'data/scores.csv' cannot be loaded: {RAGGED_CSV_MESSAGE}"


@pytest.mark.usefixtures("files_extra_missing")
async def test_get_template_without_the_files_extra_is_the_501(entry_store: RecordingStorage) -> None:
    entry_store.put("greet.j2", b"Hello {{ name }}")
    with pytest.raises(NotSupportedError) as caught:
        await get_template("greet.j2")
    assert caught.value.message == FILES_INSTALL_LINE


def test_a_template_text_entry_carries_exactly_one_of_schema_and_schema_error() -> None:
    base = {"kind": "text", "id": "a", "content_type": None, "size": 1, "content": "x"}
    TemplateTextEntry.model_validate({**base, "schema": {}, "schema_error": None})
    TemplateTextEntry.model_validate({**base, "schema": None, "schema_error": "template error: x"})
    with pytest.raises(ValidationError, match="exactly one"):
        TemplateTextEntry.model_validate({**base, "schema": {}, "schema_error": "template error: x"})
    with pytest.raises(ValidationError, match="exactly one"):
        TemplateTextEntry.model_validate({**base, "schema": None, "schema_error": None})


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


_NON_CANONICAL = ["./a", "a/../b", "a//b", "a/", "/a", ".", "a/..", "x/.", "../x"]


def _non_canonical(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


@pytest.mark.parametrize("key", _NON_CANONICAL)
async def test_every_template_door_refuses_a_non_canonical_key(manager: _ResourceManager, key: str) -> None:
    for call in (
        lambda: upload_template(key, "content"),
        lambda: delete_template(key),
        lambda: delete_template_dir(key),
        lambda: render_template(TemplatedText(id=key)),
    ):
        with pytest.raises(BadRequestError) as caught:
            await call()
        assert caught.value.message == _non_canonical(key)
    assert manager.uploaded == {}
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


# -- a held object's delete is refused --


@pytest.fixture
def held_store(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A real manager over ``jq/gate`` and ``jq/free``, recording its evictions, behind a fleet bus."""
    provider = RecordingStorage({"jq/gate": (b".a", None), "jq/free": (b".b", None)})
    evicted: list[object] = []
    bus = _install(monkeypatch, ResourceManager(provider, on_evicted=evicted.append), FakeBus(remotes=["serve-w1"]))  # type: ignore[arg-type]
    return SimpleNamespace(provider=provider, evicted=evicted, bus=bus)


_DELETES = [
    pytest.param(lambda: delete_template("jq/gate"), HELD_TEMPLATE_REFUSAL, "stored template 'jq/gate'", id="file"),
    pytest.param(lambda: delete_template_dir("jq"), HELD_DIRECTORY_REFUSAL, "directory 'jq'", id="dir"),
]


@pytest.mark.parametrize(("delete", "refusal", "head"), _DELETES)
async def test_a_held_delete_is_refused_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace, delete, refusal: str, head: str
) -> None:
    hold_jq_gate(monkeypatch)
    with pytest.raises(ConflictError) as excinfo:
        await delete()
    assert str(excinfo.value) == refusal
    assert excinfo.value.extra == {"holders": HELD_HOLDERS}
    assert sorted(held_store.provider.objects) == ["jq/free", "jq/gate"]
    assert held_store.evicted == []
    assert held_store.bus.publish_calls == []


async def test_an_unheld_delete_succeeds(monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace) -> None:
    hold_jq_gate(monkeypatch)
    assert (await delete_template("jq/free"))["deleted"] is True
    assert sorted(held_store.provider.objects) == ["jq/gate"]
    assert len(held_store.bus.publish_calls) == 1


async def test_an_unheld_directory_delete_succeeds(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace
) -> None:
    fresh_referees(monkeypatch).register(hook_referee)
    held_store.provider.objects.pop("jq/gate")
    assert (await delete_template_dir("jq"))["deleted"] is True
    assert held_store.provider.objects == {}


@pytest.mark.parametrize(("delete", "refusal", "head"), _DELETES)
async def test_a_referee_fault_fails_the_delete_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace, delete, refusal: str, head: str
) -> None:
    fresh_referees(monkeypatch).register(value_error_referee)
    with pytest.raises(OperationFailedError) as excinfo:
        await delete()
    assert str(excinfo.value) == f"{head} cannot be deleted: a holder could not be read (ValueError)"
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "'x'" not in str(excinfo.value)
    assert sorted(held_store.provider.objects) == ["jq/free", "jq/gate"]
    assert held_store.bus.publish_calls == []


@pytest.mark.parametrize(("delete", "refusal", "head"), _DELETES)
async def test_a_stored_preset_fault_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace, delete, refusal: str, head: str
) -> None:
    fresh_referees(monkeypatch).register(stored_preset_fault_referee)
    with pytest.raises(OperationFailedError) as excinfo:
        await delete()
    assert excinfo.value is STORED_PRESET_FAULT
    assert sorted(held_store.provider.objects) == ["jq/free", "jq/gate"]
    assert held_store.bus.publish_calls == []


async def test_render_template_by_id(manager: _ResourceManager) -> None:
    result = await render_template(TemplatedText(id="a.j2", kwargs={"name": "Z"}))
    assert "rendered:a.j2" in result["rendered"]


async def test_render_template_by_inline_content(manager: _ResourceManager) -> None:
    result = await render_template(TemplatedText(content="Hi {{ name }}", kwargs={"name": "Z"}))
    assert "rendered:Hi {{ name }}" in result["rendered"]


async def test_render_template_guards_a_traversing_id(manager: _ResourceManager) -> None:
    # A stored id is a caller-supplied logical key: an escaping id is a loud 400 at the
    # containment guard, before the store is reached.
    with pytest.raises(BadRequestError):
        await render_template(TemplatedText(id="../escape.j2"))


async def test_render_template_missing_id_is_not_found(
    manager: _ResourceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _missing(text, locale=None) -> str:
        raise TemplateNotFoundError(f"Template '{text.id}' not found.")

    monkeypatch.setattr(manager, "render_templated_text", _missing)
    with pytest.raises(NotFoundError, match=r"gone\.j2"):
        await render_template(TemplatedText(id="gone.j2"))


async def test_list_templates_returns_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = _ResourceManager(listed=["a.j2", "b.j2"])
    monkeypatch.setattr(templates_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=tm)))
    assert await list_templates() == ["a.j2", "b.j2"]


async def test_list_templates_leaves_out_an_unlisted_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_prefix_marks(monkeypatch)
    declare_media_prefix_marks()
    tm = _ResourceManager(listed=["inbound-media/x", "notes/b.txt"])
    _install(monkeypatch, tm)
    assert await list_templates() == ["notes/b.txt"]
    assert (await delete_template("inbound-media/x"))["deleted"] is True
    assert tm.deleted == ["inbound-media/x"]


async def test_list_templates_keeps_a_prefix_marked_only_not_backed_up(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_prefix_marks(monkeypatch).not_backed_up.declare("volatile/")
    tm = _ResourceManager(listed=["notes/b.txt", "volatile/x"])
    monkeypatch.setattr(templates_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=tm)))
    assert await list_templates() == ["notes/b.txt", "volatile/x"]


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

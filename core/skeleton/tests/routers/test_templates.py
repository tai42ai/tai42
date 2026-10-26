"""Templates router: list/get/upload/delete/render/clear-cache + the write-path
traversal guard."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from jinja2 import TemplateSyntaxError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.access_control.store as ac_store_module
import tai42_skeleton.operations.templates as templates_ops
import tai42_skeleton.routers.templates as router
import tai42_skeleton.template.path_guard as path_guard
from tai42_skeleton.app import instance
from tai42_skeleton.template import ResourceManager, TemplateNotFoundError, platform_referees
from tai42_skeleton.template.path_guard import UnsafeTemplatePathError

from .._fakes.bus import FakeBus
from .._fakes.caller_reach import bound_caller
from .._fakes.content_fixtures import (
    FILES_INSTALL_LINE,
    JAVASCRIPT,
    LOCKED_PDF_MESSAGE,
    NOTES_PDF,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    SQL,
    SQL_LATIN1,
    locked_pdf,
)
from .._fakes.recording_storage import RecordingStorage
from .._fakes.template_holders import (
    HELD_DIRECTORY_REFUSAL,
    HELD_HOLDERS,
    HELD_TEMPLATE_REFUSAL,
    fresh_referees,
    hold_jq_gate,
)


def _req(body=None) -> Request:
    async def _json():
        if isinstance(body, Exception):
            raise body
        return body

    return cast(Request, SimpleNamespace(json=_json, path_params={}, query_params={}))


def _data(resp):
    return json.loads(bytes(resp.body))


class _FakeResourceManager:
    def __init__(self):
        self.uploaded = {}
        self.deleted = []
        self.cleared = False

    async def list_resources(self):
        return ["a.j2", "b.j2"]

    async def fetch_template(self, template_id):
        return f"content of {template_id}"

    async def get_template_schema(self, content=None, template_id=None):
        return {"vars": ["name"]}

    async def upload_template(self, path, content):
        self.uploaded[path] = content

    async def delete_template(self, path):
        self.deleted.append(path)

    async def delete_template_dir(self, path):
        self.deleted.append(path)

    async def render_templated_text(self, text, locale=None):
        return f"rendered:{text.id or text.content}:{text.kwargs}"

    def clear_cache(self):
        self.cleared = True


@pytest.fixture
def manager(monkeypatch):
    tm = _FakeResourceManager()
    fake_app = SimpleNamespace(storage=SimpleNamespace(resource_manager=tm))
    # The handlers are thin adapters over operations.templates; the business logic
    # reads ``tai42_app`` from the OP module, so the fake is installed there (the
    # same module-local ``tai42_app`` symbol the handlers resolve at call time).
    monkeypatch.setattr(templates_ops, "tai42_app", fake_app)
    # The write ops broadcast the eviction over the worker bus, so a recording bus is
    # installed for the ``broadcast`` primitive; with no remotes the fan-out is local-only.
    monkeypatch.setattr(instance.app, "_bus", FakeBus())
    return tm


async def test_list(manager):
    resp = await router.list_templates(_req())
    assert _data(resp)["data"] == ["a.j2", "b.j2"]


async def test_get_missing_id_400(manager):
    resp = await router.get_template(_req({}))
    assert resp.status_code == 400


async def test_get_invalid_json_400(manager):
    resp = await router.get_template(_req(ValueError("bad json")))
    assert resp.status_code == 400
    assert "invalid JSON body" in _data(resp)["error"]


async def test_get_non_object_body_400(manager):
    resp = await router.get_template(_req("a string"))
    assert resp.status_code == 400
    assert "must be a JSON object" in _data(resp)["error"]


async def test_upload(manager):
    resp = await router.upload_template(_req({"path": "dir/x.j2", "content": "hi"}))
    assert resp.status_code == 200
    assert manager.uploaded == {"dir/x.j2": "hi"}


@pytest.mark.parametrize("bad", ["/abs.j2", "../escape.j2", "a/../../etc", "back\\slash"])
async def test_upload_traversal_rejected(manager, bad):
    resp = await router.upload_template(_req({"path": bad, "content": "x"}))
    assert resp.status_code == 400
    assert manager.uploaded == {}


async def test_upload_directory_id_collision_is_409(manager, monkeypatch):
    # An id that still names a directory holding templates can't also become a
    # file: the provider's typed conflict maps to a 409 naming the templates.
    from tai42_contract.storage import StoragePathConflictError

    async def _conflict(path, content):
        raise StoragePathConflictError(path, ["dir/x.j2", "dir/y.j2"])

    monkeypatch.setattr(manager, "upload_template", _conflict)
    resp = await router.upload_template(_req({"path": "dir", "content": "hi"}))
    assert resp.status_code == 409
    body = _data(resp)
    assert "dir/x.j2" in body["error"]
    assert "delete them first" in body["error"]


@pytest.mark.parametrize(
    ("handler", "path", "refusal"),
    [
        (router.delete_template, "jq/gate", HELD_TEMPLATE_REFUSAL),
        (router.delete_template_dir, "jq", HELD_DIRECTORY_REFUSAL),
    ],
    ids=["file", "dir"],
)
@pytest.mark.usefixtures("admin_caller")
async def test_a_held_delete_is_409_naming_its_holders(monkeypatch, handler, path, refusal):
    provider = RecordingStorage({"jq/gate": (b".a", None)})
    fake_app = SimpleNamespace(
        storage=SimpleNamespace(
            resource_manager=ResourceManager(provider, on_evicted=instance.app._fire_template_evicted)
        )
    )
    monkeypatch.setattr(templates_ops, "tai42_app", fake_app)
    monkeypatch.setattr(instance.app, "_bus", FakeBus())
    hold_jq_gate(monkeypatch)
    resp = await handler(_req({"path": path}))
    assert resp.status_code == 409
    assert _data(resp) == {"error": refusal, "holders": HELD_HOLDERS}
    assert list(provider.objects) == ["jq/gate"]


_POLICY_ROWS = [
    ("alice", {"condition": {"id": "jq/gate"}, "policy_data": {}}),
    ("key-1", {"condition": {"id": "jq/gate"}, "policy_data": {KEY_FINGERPRINT_CLAIM: "fp"}}),
]


def _identities_name_jq_gate(monkeypatch) -> RecordingStorage:
    """A stored ``jq/gate`` that principal ``alice`` and api key ``key-1`` name, read by the
    platform's access-control referee as the only template-delete referee."""
    provider = RecordingStorage({"jq/gate": (b".a", None)})
    fake_app = SimpleNamespace(
        storage=SimpleNamespace(
            resource_manager=ResourceManager(provider, on_evicted=instance.app._fire_template_evicted)
        )
    )
    monkeypatch.setattr(templates_ops, "tai42_app", fake_app)
    monkeypatch.setattr(instance.app, "_bus", FakeBus())

    async def policies():
        return _POLICY_ROWS

    monkeypatch.setattr(platform_referees, "_skeleton_store_configured", lambda: True)
    monkeypatch.setattr(
        ac_store_module, "access_control_store", lambda: SimpleNamespace(list_conditioned_policies=policies)
    )
    fresh_referees(monkeypatch).register(platform_referees._access_control_referee)
    return provider


async def test_a_role_member_refused_delete_counts_the_identities_and_reads_who_removes_them(monkeypatch):
    provider = _identities_name_jq_gate(monkeypatch)
    with bound_caller(monkeypatch, {"templates": "write"}):
        resp = await router.delete_template(_req({"path": "jq/gate"}))
    assert resp.status_code == 409
    body = _data(resp)
    assert body["error"] == (
        "stored template 'jq/gate' cannot be deleted — held by:\n"
        "- principal: 1 not readable with your role\n"
        "- api key: 1 not readable with your role\n"
        "ask someone with access to 'access-control' to remove those references first"
    )
    assert not any(user in json.dumps(body) for user in ("alice", "key-1"))
    assert list(provider.objects) == ["jq/gate"]


def test_with_access_control_off_a_refused_delete_over_http_names_the_identities(monkeypatch):
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    reset_all_settings()
    provider = _identities_name_jq_gate(monkeypatch)
    app = Starlette(
        routes=[Route("/api/delete-template", router.delete_template, methods=["POST"])],
        middleware=instance.app._base_middleware(None, stream_path="/mcp"),
    )
    try:
        with TestClient(app) as client:
            resp = client.post("/api/delete-template", json={"path": "jq/gate"})
    finally:
        reset_all_settings()
    assert resp.status_code == 409
    assert resp.json()["error"] == (
        "stored template 'jq/gate' cannot be deleted — held by:\n"
        "- principal 'alice' names 'jq/gate' at condition\n"
        "- api key 'key-1' names 'jq/gate' at condition\n"
        "remove those references first"
    )
    assert list(provider.objects) == ["jq/gate"]


async def test_delete_traversal_rejected(manager):
    resp = await router.delete_template(_req({"path": "../secret"}))
    assert resp.status_code == 400
    assert manager.deleted == []


async def test_delete(manager):
    resp = await router.delete_template(_req({"path": "x.j2"}))
    assert resp.status_code == 200
    assert manager.deleted == ["x.j2"]


async def test_delete_absent_path_is_idempotent_200(manager, monkeypatch):
    # Deleting a path that is already absent is idempotent: the store treats a
    # missing template as a no-op (it does NOT raise), so the route returns 200,
    # not 404.
    seen: list[str] = []

    async def _noop_delete(path):
        # A store that is idempotent for an absent path: succeeds without raising.
        seen.append(path)

    monkeypatch.setattr(manager, "delete_template", _noop_delete)
    resp = await router.delete_template(_req({"path": "never-existed.j2"}))
    assert resp.status_code == 200
    data = _data(resp)["data"]
    assert data["path"] == "never-existed.j2"
    assert data["deleted"] is True
    assert seen == ["never-existed.j2"]


async def test_delete_dir(manager):
    resp = await router.delete_template_dir(_req({"path": "prompts/archive"}))
    assert resp.status_code == 200
    data = _data(resp)["data"]
    assert data["path"] == "prompts/archive"
    assert data["deleted"] is True
    assert manager.deleted == ["prompts/archive"]


@pytest.mark.parametrize("bad", ["/abs", "../escape", "a/../../etc", "back\\slash"])
async def test_delete_dir_traversal_rejected(manager, bad):
    resp = await router.delete_template_dir(_req({"path": bad}))
    assert resp.status_code == 400
    assert manager.deleted == []


async def test_delete_dir_absent_is_404(manager, monkeypatch):
    # A directory that matches nothing is a loud 404, not a silent no-op: the
    # provider raises ``FileNotFoundError`` and the op maps it to a 404.
    async def _absent(path):
        raise FileNotFoundError(f"Storage directory not found: {path}")

    monkeypatch.setattr(manager, "delete_template_dir", _absent)
    resp = await router.delete_template_dir(_req({"path": "prompts/gone"}))
    assert resp.status_code == 404
    assert "not found" in _data(resp)["error"]


_NON_CANONICAL = ["./a", "a/../b", "a//b", "a/", "/a", ".", "a/.."]


def _non_canonical(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


@pytest.mark.parametrize("key", _NON_CANONICAL)
async def test_every_template_route_refuses_a_non_canonical_key(manager, key):
    responses = [
        await router.upload_template(_req({"path": key, "content": "x"})),
        await router.delete_template(_req({"path": key})),
        await router.delete_template_dir(_req({"path": key})),
        await router.get_template(_req({"template_id": key})),
        await router.render_template(_req({"text": {"id": key}})),
    ]
    for resp in responses:
        assert resp.status_code == 400
        assert _data(resp) == {"error": _non_canonical(key)}
    assert manager.uploaded == {}
    assert manager.deleted == []


async def test_a_canonical_nested_key_passes_the_template_routes(manager):
    resp = await router.upload_template(_req({"path": "a/b.txt", "content": "x"}))
    assert resp.status_code == 200
    assert manager.uploaded == {"a/b.txt": "x"}


async def test_list_leaves_out_an_unlisted_prefix(manager, monkeypatch):
    from tai42_skeleton.conversations.media_meta import declare_media_prefix_marks

    from .._fakes.prefix_marks import fresh_prefix_marks

    fresh_prefix_marks(monkeypatch)
    declare_media_prefix_marks()

    async def _listed():
        return ["inbound-media/x", "notes/b.txt"]

    monkeypatch.setattr(manager, "list_resources", _listed)
    resp = await router.list_templates(_req())
    assert _data(resp)["data"] == ["notes/b.txt"]


@pytest.mark.parametrize("bad", ["/abs.j2", "../escape.j2", "a/../../etc", "back\\slash"])
async def test_get_template_id_traversal_rejected(manager, bad):
    resp = await router.get_template(_req({"template_id": bad}))
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", ["/abs.j2", "../escape.j2", "a/../../etc", "back\\slash"])
async def test_render_template_id_traversal_rejected(manager, bad):
    resp = await router.render_template(_req({"text": {"id": bad}}))
    assert resp.status_code == 400


async def test_render_legit_template_id_passes(manager):
    resp = await router.render_template(_req({"text": {"id": "dir/ok.j2"}}))
    assert resp.status_code == 200


def test_safe_path_symlink_escape_rejected(monkeypatch, tmp_path):
    """With ``_TEMPLATE_ROOT`` monkeypatched to a REAL directory (as a
    filesystem-backed store's own root would be), a key through a component that
    symlinks OUTSIDE the root resolves out via realpath and is refused. This
    exercises realpath's symlink handling against a real root — production uses a
    virtual (non-existent) anchor, so its guard is lexical only and this branch
    never fires there; the store layer owns symlink defense for a real root."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.j2").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside)  # a component under-root that escapes
    monkeypatch.setattr(path_guard, "_TEMPLATE_ROOT", str(root))

    with pytest.raises(UnsafeTemplatePathError):
        path_guard.safe_template_path("link/secret.j2")


def test_safe_path_symlink_inside_root_passes(monkeypatch, tmp_path):
    """With ``_TEMPLATE_ROOT`` monkeypatched to a REAL directory, a symlink that
    stays INSIDE the root resolves under-root and is allowed — the guard rejects
    escapes, not every symlink."""
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "t.j2").write_text("hi", encoding="utf-8")
    (root / "link").symlink_to(root / "real")
    monkeypatch.setattr(path_guard, "_TEMPLATE_ROOT", str(root))

    assert path_guard.safe_template_path("link/t.j2") == "link/t.j2"


async def test_render_requires_source(manager):
    resp = await router.render_template(_req({}))
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", [123, {"nested": "object"}, ["a", "b"], True])
async def test_render_non_string_content_rejected(manager, bad):
    """A non-string inline ``content`` is refused with a clean 400 before it can
    reach Jinja and surface as a 500."""
    resp = await router.render_template(_req({"text": {"content": bad}}))
    assert resp.status_code == 400


async def test_render(manager):
    resp = await router.render_template(_req({"text": {"id": "a.j2", "kwargs": {"name": "Z"}}}))
    assert "rendered:a.j2" in _data(resp)["data"]["rendered"]


async def test_clear_cache(manager):
    resp = await router.clear_templates_cache(_req())
    assert _data(resp)["data"]["cleared"] is True
    assert manager.cleared is True


# --- error mapping: 404 on missing, 400 on author error, 500 on infra --------


async def test_render_missing_template_404(manager, monkeypatch):
    async def _missing(text, locale=None):
        raise TemplateNotFoundError(f"Template '{text.id}' not found.")

    monkeypatch.setattr(manager, "render_templated_text", _missing)
    resp = await router.render_template(_req({"text": {"id": "gone.j2"}}))
    assert resp.status_code == 404


async def test_render_bad_jinja_400(manager, monkeypatch):
    async def _broken(text, locale=None):
        raise TemplateSyntaxError("bad syntax", 1)

    monkeypatch.setattr(manager, "render_templated_text", _broken)
    resp = await router.render_template(_req({"text": {"content": "{{ oops"}}))
    assert resp.status_code == 400
    assert "template error" in _data(resp)["error"]


async def test_render_both_content_and_id_400(manager):
    resp = await router.render_template(_req({"text": {"content": "hi", "id": "a.j2"}}))
    assert resp.status_code == 400
    assert "not both" in _data(resp)["error"]


async def test_render_infra_error_propagates_as_500(manager, monkeypatch):
    # A genuine storage/infra failure is NOT an author error: it must propagate
    # (surfacing as a 500), never be masked as a 400/404.
    async def _boom(text, locale=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr(manager, "render_templated_text", _boom)
    with pytest.raises(RuntimeError, match="redis down"):
        await router.render_template(_req({"text": {"id": "a.j2"}}))


# --- door-level: the REAL ResourceManager behind the route -------------------


@pytest.fixture
def real_manager(monkeypatch) -> RecordingStorage:
    """The route wired to a REAL ResourceManager over a recording in-memory provider.

    The fake manager above stubs ``get_template_schema``, so it can never catch a
    schema-inference failure; this fixture drives the genuine reader and inference path.
    """
    provider = RecordingStorage()
    monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
    tm = ResourceManager(provider, on_evicted=lambda _eviction: None)
    monkeypatch.setattr(templates_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=tm)))
    monkeypatch.setattr(instance.app, "_bus", FakeBus())
    return provider


async def _entry(template_id: str):
    return await router.get_template(_req({"template_id": template_id}))


async def test_get_answers_the_template_entry_shape(real_manager):
    real_manager.put("a.j2", b"Hello {{ name }}", "text/plain")
    resp = await _entry("a.j2")
    assert resp.status_code == 200
    body = _data(resp)["data"]
    assert set(body) == {"kind", "id", "content_type", "size", "content", "schema", "schema_error"}
    assert body["kind"] == "text"
    assert body["content"] == "Hello {{ name }}"
    assert body["schema"]["required"] == ["name"]
    assert body["schema_error"] is None


async def test_get_real_manager_uninferable_template_200_degraded(real_manager):
    """A valid, renderable template that jinja2schema cannot infer reads back 200
    with a degraded schema — not the 500 the op's declared errors forbid."""
    upload = await router.upload_template(_req({"path": "d.j2", "content": "{{ {'a': 1} }}{{ who }}"}))
    assert upload.status_code == 200

    resp = await _entry("d.j2")

    assert resp.status_code == 200
    body = _data(resp)["data"]
    assert body["content"] == "{{ {'a': 1} }}{{ who }}"
    assert body["schema"]["x-tai42-inference"] == "partial"
    assert body["schema"]["properties"] == {"who": {"title": "who"}}


async def test_get_real_manager_inferable_template_200_rich(real_manager):
    """The enricher is untouched for a template jinja2schema understands."""
    await router.upload_template(_req({"path": "r.j2", "content": "{{ name }}"}))

    resp = await _entry("r.j2")

    assert resp.status_code == 200
    schema = _data(resp)["data"]["schema"]
    assert "x-tai42-inference" not in schema
    assert schema["required"] == ["name"]


async def test_get_broken_jinja_serves_the_content_with_the_schema_error(real_manager):
    await router.upload_template(_req({"path": "b.j2", "content": "Hello {{ name }"}))

    resp = await _entry("b.j2")

    assert resp.status_code == 200
    body = _data(resp)["data"]
    assert body["content"] == "Hello {{ name }"
    assert body["schema"] is None
    assert body["schema_error"] == "template error: unexpected '}'"


async def test_get_missing_template_404(real_manager):
    resp = await _entry("gone.j2")
    assert resp.status_code == 404
    assert _data(resp) == {"error": "resource 'gone.j2' not found"}


async def test_get_sql_and_javascript_are_text_entries(real_manager):
    real_manager.put("queries/lookup.sql", SQL)
    real_manager.put("app/greet.js", JAVASCRIPT)
    sql = _data(await _entry("queries/lookup.sql"))["data"]
    assert (sql["kind"], sql["content"]) == ("text", SQL.decode())
    assert list(sql["schema"]["properties"]) == ["account_id"]
    script = _data(await _entry("app/greet.js"))["data"]
    assert (script["kind"], script["content"]) == ("text", JAVASCRIPT.decode())


async def test_get_empty_object_is_empty_text(real_manager):
    real_manager.put("empty.j2", b"")
    body = _data(await _entry("empty.j2"))["data"]
    assert (body["kind"], body["content"]) == ("text", "")


async def test_get_pdf_is_a_document_with_a_null_schema(real_manager):
    real_manager.put("docs/notes.pdf", NOTES_PDF, "application/pdf")
    body = _data(await _entry("docs/notes.pdf"))["data"]
    assert body["kind"] == "document"
    assert "Quarterly notes" in body["content"]
    assert body["schema"] is None


async def test_get_refusals_answer_400_with_their_text(real_manager):
    real_manager.put("queries/latin.sql", SQL_LATIN1)
    real_manager.put("docs/locked.pdf", locked_pdf())
    real_manager.put("data/scores.csv", RAGGED_CSV)
    latin = await _entry("queries/latin.sql")
    assert latin.status_code == 400
    assert _data(latin)["error"].startswith("resource 'queries/latin.sql' cannot be loaded: ")
    locked = await _entry("docs/locked.pdf")
    assert (locked.status_code, _data(locked)) == (
        400,
        {"error": f"resource 'docs/locked.pdf' cannot be loaded: {LOCKED_PDF_MESSAGE}"},
    )
    ragged = await _entry("data/scores.csv")
    assert (ragged.status_code, _data(ragged)) == (
        400,
        {"error": f"resource 'data/scores.csv' cannot be loaded: {RAGGED_CSV_MESSAGE}"},
    )


@pytest.mark.usefixtures("files_extra_missing")
async def test_get_without_the_files_extra_is_501_while_a_by_id_render_still_renders(real_manager):
    real_manager.put("greet.j2", b"Hello {{ name }}")
    resp = await _entry("greet.j2")
    assert resp.status_code == 501
    assert _data(resp) == {"error": FILES_INSTALL_LINE, "code": "files-not-configured"}
    render = await router.render_template(_req({"text": {"id": "greet.j2", "kwargs": {"name": "Ada"}}}))
    assert render.status_code == 200
    assert _data(render)["data"]["rendered"] == "Hello Ada"

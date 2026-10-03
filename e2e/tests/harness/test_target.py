"""Harness self-test for the e2e target (``TAI_E2E_TARGET``): the needs vocabulary, the
target file, the match of a test's needs against a target's facts, and the stack handle
of a run against a target."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from tai42_e2e import ports
from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.booting import boot_stack
from tai42_e2e.harness import connect_infra
from tai42_e2e.manifests import build_core_stack, build_replicas_stack
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.target import Target, TargetError, check_needs, fetch_kinds, load_target

# Pure harness self-test: it drives a stand-in target, never a stack of the product.
pytestmark = [pytest.mark.backendless, pytest.mark.needs("no-stack")]

_KEY = "sk-target-test"
_KINDS = [
    {"kind": "storage", "state": "active", "plugin": "tai42_storage_local", "detail": "LocalStorage"},
    {"kind": "channels", "state": "active", "plugin": None, "detail": "channels: web, relay"},
    {"kind": "config", "state": "default", "plugin": None, "detail": "mode: file"},
    {"kind": "accounts", "state": "off", "plugin": None, "detail": "no accounts provider registered"},
]


@pytest.fixture
def target_url() -> Iterator[str]:
    """A stand-in for a running stack: ``/health``, and ``/api/system/kinds`` behind the key."""
    app = FastAPI()

    @app.get("/health")
    def health() -> PlainTextResponse:
        return PlainTextResponse("OK")

    @app.get("/api/system/kinds")
    def kinds(request: Request) -> JSONResponse:
        if request.headers.get("authorization") != f"Bearer {_KEY}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return JSONResponse({"data": _KINDS})

    port = ports.allocate_port()
    try:
        with ThreadedServer(app, "127.0.0.1", port):
            yield f"http://127.0.0.1:{port}"
    finally:
        ports.release_port(port)


def _known() -> Target:
    return Target("https://stack.example.com", provides=frozenset({"probe-tools"})).with_kinds(_KINDS)


def test_needs_outside_the_vocabulary_raise() -> None:
    check_needs(["kind:storage", "kind:channels:web", "probe-tools", "mutable", "process", "helper:llm", "no-stack"])
    for unknown in ("storage", "kind", "kind:", "probe_tools", "helpers:llm"):
        with pytest.raises(TargetError, match="unknown need"):
            check_needs([unknown])


@pytest.mark.parametrize(
    ("needs", "reason"),
    [
        ([], None),
        (["kind:storage", "kind:config", "kind:channels:web", "probe-tools"], None),
        (None, "no needs declared"),
        (["kind:accounts"], "kind accounts off"),
        (["kind:sandbox"], "kind sandbox off"),
        (["kind:channels:telegram"], "does not name telegram"),
        (["mutable"], "does not provide mutable"),
        (["kind:storage", "store:redis"], "needs store:redis"),
        (["process"], "only a stack this run builds"),
    ],
)
def test_needs_are_matched_against_the_target_facts(needs: list[str] | None, reason: str | None) -> None:
    unmet = _known().unmet(needs)
    if reason is None:
        assert unmet is None
    else:
        assert unmet is not None
        assert reason in unmet


def test_bare_origin_is_a_target_with_no_file(tmp_path: Path) -> None:
    target = load_target("https://stack.example.com/", tmp_path)
    assert (target.url, target.key_env, target.provides) == ("https://stack.example.com", "TAI_E2E_KEY", frozenset())


def test_named_target_is_read_from_its_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "alpha.yml").write_text(
        "url: http://127.0.0.1:8000\nkey_env: ALPHA_KEY\nprovides:\n  - mutable\n", encoding="utf-8"
    )
    monkeypatch.setenv("ALPHA_KEY", _KEY)
    for value in ("alpha", str(tmp_path / "alpha.yml")):
        target = load_target(value, tmp_path)
        assert (target.url, target.key, target.provides) == ("http://127.0.0.1:8000", _KEY, frozenset({"mutable"}))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("key_env: K\n", "mapping with a `url`"),
        ("url: stack.example.com\n", "http\\(s\\) origin"),
        ("url: http://host/path\n", "http\\(s\\) origin"),
        ("url: http://u:p@host\n", "credentials"),
        ("url: http://host\nkey: sk-inline\n", "unknown key"),
        ("url: http://host\nprovides: [store]\n", "unknown fact"),
    ],
)
def test_malformed_target_file_raises(tmp_path: Path, document: str, message: str) -> None:
    (tmp_path / "bad.yml").write_text(document, encoding="utf-8")
    with pytest.raises(TargetError, match=message):
        load_target("bad", tmp_path)


def test_missing_target_file_raises(tmp_path: Path) -> None:
    with pytest.raises(TargetError, match="no target file"):
        load_target("absent", tmp_path)


def test_blank_target_setting_is_unset() -> None:
    assert HarnessSettings(target="  ").target is None


def test_kinds_are_fetched_with_the_key(target_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_E2E_KEY", _KEY)
    target = fetch_kinds(Target(target_url))
    assert set(target.kinds) == {"storage", "channels", "config"}
    assert "web" in target.kinds["channels"]


def test_kinds_refused_without_the_key_raise(target_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_E2E_KEY", raising=False)
    with pytest.raises(TargetError, match="TAI_E2E_KEY"):
        fetch_kinds(Target(target_url))


async def test_stack_of_a_target_run_addresses_the_target(
    target_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAI_E2E_KEY", _KEY)
    infra = connect_infra(HarnessSettings(), Target(target_url))
    for builder in (build_core_stack, build_replicas_stack):
        with contextlib.contextmanager(boot_stack)(infra, tmp_path, builder, seed_auth=True) as stack:
            assert stack.origin() == stack.origin(stack.port_a) == stack.origin(stack.port_b) == target_url
            assert (await stack.api().request_raw("GET", "/api/system/kinds")).status_code == 200
            for member in ("config", "resources", "infra"):
                with pytest.raises(AttributeError, match="built no stack"):
                    getattr(stack, member)
            with pytest.raises(AttributeError, match="built no stack"):
                stack.restart("serve")
    assert not list(tmp_path.iterdir())


def test_unreachable_target_fails_boot(tmp_path: Path) -> None:
    port = ports.allocate_port()
    try:
        infra = connect_infra(HarnessSettings(boot_timeout=1.0), Target(f"http://127.0.0.1:{port}"))
        with (
            pytest.raises(TimeoutError, match="never became healthy"),
            contextlib.contextmanager(boot_stack)(infra, tmp_path, build_core_stack),
        ):
            pass
    finally:
        ports.release_port(port)

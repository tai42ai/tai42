"""The e2e target: a running stack a run drives instead of building one.

``TAI_E2E_TARGET`` (:attr:`HarnessSettings.target`) names it: a bare origin
(``https://stack.example.com``) or a target file (``staging`` resolves to
``targets/staging.yml`` beside the suite). A target file holds the origin, the NAME of
the environment variable carrying the login key, and the facts the stack cannot report
about itself; what it can report is read once from its ``GET /api/system/kinds``.

Every test declares what it needs from a stack (the ``needs`` marker). Against a target
each test either fits the target's facts and runs, or is skipped with the missing fact as
the reason. The vocabulary has three classes, decided by the word before the first ``:``:

* reported — ``kind:<kind>`` (that kind is on) and ``kind:<kind>:<name>`` (and ``<name>``
  appears in its plugin or detail), read from the kinds endpoint;
* declared — :data:`DECLARED_NEEDS`, true only when the target file lists it under
  ``provides``;
* built — :data:`BUILT_NEEDS`, which only a stack the run builds itself has, so a test
  that needs one never runs against a target. A qualifier after ``:`` is free text for the
  reader (``helper:llm``, ``setting:ACCESS_CONTROL_ENABLE=false``).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.mcp import McpClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for

DEFAULT_KEY_ENV = "TAI_E2E_KEY"

# Facts a target file may list under ``provides``.
DECLARED_NEEDS: Mapping[str, str] = {
    "probe-tools": "the e2e probe tools are loaded in the stack",
    "mutable": "tests may change stack-wide state",
}

# Needs only a stack the run builds itself can meet.
BUILT_NEEDS: Mapping[str, str] = {
    "process": "control of the stack's processes",
    "store": "direct access to the stack's Redis or Postgres",
    "files": "the stack's files on disk",
    "helper": "a local helper service the test controls",
    "setting": "a specific setting of the stack",
    "topology": "a specific process topology",
    "cli": "the CLI run beside the stack",
    "metrics": "the standalone metrics process",
    "second-stack": "a second stack",
    "fixture-page": "the suite's own fixture page",
    "no-stack": "no running stack at all",
}

_TARGET_FILE_KEYS = frozenset({"url", "key_env", "provides"})


class TargetError(ValueError):
    """A target that cannot be used as named: a malformed origin or file, an unknown
    declared fact, a missing login key, or a stack that does not answer its kinds."""


def check_needs(needs: Iterable[str]) -> None:
    """Raise :class:`TargetError` naming every need outside the vocabulary."""
    unknown = sorted(need for need in needs if _need_class(need) is None)
    if unknown:
        raise TargetError(
            f"unknown need(s) {', '.join(unknown)}; a need is kind:<kind>[:<name>], one of "
            f"{', '.join(sorted(DECLARED_NEEDS))}, or one of {', '.join(sorted(BUILT_NEEDS))} (optionally :<detail>)"
        )


def _need_class(need: str) -> str | None:
    head, _, tail = need.partition(":")
    if head == "kind":
        return "reported" if tail else None
    if need in DECLARED_NEEDS:
        return "declared"
    if head in BUILT_NEEDS:
        return "built"
    return None


def _origin(value: str, source: str) -> str:
    origin = value.strip().rstrip("/")
    parts = urlsplit(origin)
    try:
        parts.port  # noqa: B018 - the property raises on a malformed port
    except ValueError as exc:
        raise TargetError(f"{source}: invalid port in {value!r}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment:
        raise TargetError(f"{source}: expected an http(s) origin like https://host:port, got {value!r}")
    if parts.username or parts.password:
        raise TargetError(f"{source}: the origin must not carry credentials")
    return origin


@dataclass(frozen=True)
class Target:
    """One running stack and the facts known about it before it is asked."""

    url: str
    key_env: str = DEFAULT_KEY_ENV
    provides: frozenset[str] = frozenset()
    # The kinds the stack reports as on, each mapped to the lowercased plugin + detail
    # text of its row. Filled by :meth:`with_kinds`.
    kinds: Mapping[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str | None:
        """The login key, read from the environment variable the target names."""
        return os.environ.get(self.key_env) or None

    def with_kinds(self, rows: Iterable[Mapping[str, Any]]) -> Target:
        """This target plus what its kinds endpoint reported."""
        on = {
            str(row["kind"]): f"{row.get('plugin') or ''} {row.get('detail') or ''}".lower()
            for row in rows
            if row.get("state") != "off"
        }
        return Target(self.url, self.key_env, self.provides, on)

    def unmet(self, needs: Iterable[str] | None) -> str | None:
        """Why a test with these needs cannot run against this target, or ``None`` when it
        can. ``None`` needs means the test declared nothing."""
        if needs is None:
            return "no needs declared: built-stack only"
        for need in needs:
            head, _, tail = need.partition(":")
            if head in BUILT_NEEDS:
                return f"needs {need} ({BUILT_NEEDS[head]}): only a stack this run builds has it"
            if need in DECLARED_NEEDS:
                if need not in self.provides:
                    return f"target does not provide {need} ({DECLARED_NEEDS[need]})"
                continue
            kind, _, name = tail.partition(":")
            if kind not in self.kinds:
                return f"target reports kind {kind} off"
            if name and name.lower() not in self.kinds[kind]:
                return f"target's {kind} does not name {name}"
        return None


def load_target(value: str, targets_dir: Path) -> Target:
    """Resolve ``TAI_E2E_TARGET``: an http(s) origin is a target with no file; a value
    ending in ``.yml`` is a target file path; anything else names
    ``<targets_dir>/<value>.yml``."""
    if value.startswith(("http://", "https://")):
        return Target(_origin(value, "TAI_E2E_TARGET"))
    path = Path(value) if value.endswith(".yml") else targets_dir / f"{value}.yml"
    if not path.is_file():
        raise TargetError(f"TAI_E2E_TARGET={value!r}: no target file at {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "url" not in document:
        raise TargetError(f"{path}: a target file is a mapping with a `url`")
    unknown_keys = sorted(set(document) - _TARGET_FILE_KEYS)
    if unknown_keys:
        raise TargetError(f"{path}: unknown key(s) {', '.join(unknown_keys)}")
    provides = document.get("provides") or []
    if not isinstance(provides, list):
        raise TargetError(f"{path}: `provides` is a list")
    unknown_facts = sorted(str(fact) for fact in provides if fact not in DECLARED_NEEDS)
    if unknown_facts:
        raise TargetError(
            f"{path}: `provides` lists unknown fact(s) {', '.join(unknown_facts)}; "
            f"a target can declare {', '.join(sorted(DECLARED_NEEDS))}"
        )
    return Target(
        _origin(str(document["url"]), str(path)),
        str(document.get("key_env") or DEFAULT_KEY_ENV),
        frozenset(str(fact) for fact in provides),
    )


def fetch_kinds(target: Target) -> Target:
    """Ask the target which kinds are on (one ``GET /api/system/kinds`` with the login
    key) and return the target carrying the answer."""
    headers = {"Authorization": f"Bearer {target.key}"} if target.key else {}
    url = f"{target.url}/api/system/kinds"
    try:
        response = httpx.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as exc:
        raise TargetError(f"the e2e target did not answer {url}: {exc!r}") from exc
    if response.status_code in (401, 403):
        raise TargetError(
            f"the e2e target refused {url} ({response.status_code}): set the login key in ${target.key_env}"
        )
    if response.status_code != 200:
        raise TargetError(f"the e2e target answered {url} with {response.status_code}")
    return target.with_kinds(response.json()["data"])


class TargetStack(TaiStack):
    """The stack handle of a run against a target. Its clients address the target's
    origin with the login key; it spawned nothing and owns no stores, so every other
    member of :class:`TaiStack` raises."""

    def __init__(self, target: Target, *, boot_timeout: float) -> None:
        parts = urlsplit(target.url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        self.target = target
        self.host = parts.hostname or ""
        self.app_ports = [port, port]
        self.auth_token = target.key
        self._boot_timeout = boot_timeout

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"TaiStack.{name} is unavailable: this run targets {self.target.url} and built no stack")

    def boot(self) -> None:
        """Wait for the target's ``/health``."""

        def healthy() -> bool:
            try:
                return httpx.get(f"{self.target.url}/health", timeout=2.0).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(
            healthy, deadline=self._boot_timeout, message=f"the e2e target never became healthy at {self.target.url}"
        )

    def teardown(self) -> None:
        """Nothing was spawned or allocated."""

    def origin(self, port: int | None = None) -> str:
        del port
        return self.target.url

    def mcp(self, port: int | None = None, path: str = "/mcp", *, auth: str | None = None) -> McpClient:
        del port
        return McpClient(f"{self.target.url}{path}", auth=auth if auth is not None else self.auth_token)

    def api(self, port: int | None = None) -> ApiClient:
        del port
        return ApiClient(self.target.url, auth_token=self.auth_token)

#!/usr/bin/env python3
"""Consumer boot gate: fail a platform release whose candidate core breaks the
BOOT of a previously published consumer — a behavioural check the textual
API-diff gate cannot see.

The API-diff gate (``tai42_cli.api_gate``) classifies a public-symbol diff. A change
that removes no symbol yet refuses a previously valid route or lifecycle at
startup reads as additive to that gate, while every published consumer built on
the old surface fails to boot. This gate closes that hole: it installs the
candidate tree's core packages (contract/kit/skeleton/cli, editable from the
tree) together with one or more PREVIOUSLY PUBLISHED consumer distributions,
boots the app the real way (the ``tai serve`` entrypoint, access control on,
health awaited), and asserts, for each consumer, that every route registers, no
lifecycle handler raises, and health becomes ready.

Each consumer is mounted the way a deployment layers it: its additive surface
(routers/tools/channels/extensions/identity providers/lifecycle) plus the exclusive
provider slot it selects — ``backend_module`` / ``storage_module`` / ``sandbox_module``
/ ``monitoring_module`` or an ``agents`` entry per agent — so the plugin's module is
imported and registers, and health-ready proves that registration ran. A provider whose
boot lifecycle needs a live external service CI has no stand-in for (a config provider,
selected before the manifest and backed by e.g. the Kubernetes API) is INSTALLED and
reported ``install-only``, never booted — the summary line distinguishes the two, so a
consumer the gate cannot boot is never a silent pass.

A consumer that declares ``permissions.network`` (it reaches an external messaging API
or identity provider at startup) is given the deployment config it needs with every
outbound endpoint pointed at a closed loopback port. Its boot then runs through install,
import, registration and lifecycle and either becomes health-ready (its startup contacts
nothing, e.g. a config-guard-only channel) or fails ONLY with a connection-class error at
that endpoint. The latter is reported ``install-only (external service: <handler>)`` — the
plugin needs a live external service the gate cannot provide, distinct from a candidate
break (a missing symbol, a refused route, a guard). The
install-only-vs-broken decision reads only the declared network permission and the runtime
error class, never a distribution name.

Rule: a boot failure of a previously published consumer against the candidate is
a BREAKING change. The gate fails unless the governing release is a MAJOR bump —
the bump read the same way ``tai42_cli.api_gate`` reads it, the governing package's
version against its previous released tag. Under a major bump the same failure is
reported as an ACCEPTED break (a printed notice) rather than a gate failure.

A consumer whose declared dependency range excludes the candidate cannot even be
installed into the boot venv; the resolver's ``No solution found`` is the most
explicit form of the same break and is classified identically — accepted under a
major bump, a gate failure otherwise. Any other install failure (network, a bad
wheel, a build error) is always a hard failure, never reclassified.

Consumers come from two sources. Supplied ones are wheels (``--consumer-wheel``)
or requirement specs (``--consumer-req``); the gate names no specific consumer, so
it is agnostic to which distributions a deployment layers on the platform. The
first-party plugins are enumerated with ``--emit-matrix``: each packaged plugin at
its latest published PyPI version that is NOT in this train's bump set (a plugin
being released now boots its unpublished candidate code through its own lanes,
never here), one boot per consumer so mutually exclusive infra plugins never share
a manifest. When no consumer is supplied (a first release, or a run without the
download secret) the gate says so and passes; a plugin with no PyPI release yet is
a skipped notice, never a failure. A supplied wheel that does not exist raises
loudly — a download that failed upstream can never read as a silent pass.

CLI::

    consumer_boot_gate.py --package tai42-skeleton --dir core/skeleton \\
        --version 11.2.0 --consumer-wheel dist/some_consumer-1.4.0-py3-none-any.whl
    consumer_boot_gate.py --package tai42-skeleton --dir core/skeleton \\
        --consumer-req tai42-some-plugin==1.4.0
    consumer_boot_gate.py --emit-matrix   # a CI matrix of the first-party consumers
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from tai42_cli import api_gate

# The core packages installed editable from the candidate tree: the platform the
# consumers boot against. Overridable so a fork with a different layout can point
# the gate at its own core member dirs.
_DEFAULT_CORE_DIRS = ("core/contract", "core/kit", "core/skeleton", "core/cli")
# Kit's optional-dependency groups a served app with a consumer needs present: the
# jq/llm engines, the Postgres and Redis clients, the ASGI server, curl transport,
# and the LangGraph Postgres checkpoint saver.
_KIT_EXTRAS = "jq,llm,postgres,redis,langgraph-checkpoint-postgres,uvicorn,curl"
# The access-control identity provider the boot posture uses. Access control is on
# so the startup route-resolution guards run (the surface that refuses a malformed
# route); a provider is required for that posture.
_DEFAULT_IDENTITY_PACKAGE = "tai42-identity-redis"
_IDENTITY_PROVIDER_NAME = "redis"
_IDENTITY_LIFECYCLE_MODULE = "tai42_identity_redis"

_HEALTH_DEADLINE_S = 90.0
_WHEEL_NAME_RE = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*)-")
_HANDLER_RE = re.compile(r"(\w+): (?:\w+\.)*\w*(?:Error|Exception|Exit)\(")
_ROUTE_RE = re.compile(r"\b([A-Z]{3,7}) (/\S+?)(?=[\s'\"]|$)")


def _fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


# ------------------------------------------------------------------ bump verdict


def read_project_version(member_dir: Path) -> str:
    """The ``project.version`` of a packaged member — the source of truth the
    release tag must match, read the same way the release workflow reads it."""
    import tomllib

    pyproject = member_dir / "pyproject.toml"
    if not pyproject.is_file():
        _fail(f"no pyproject.toml at {pyproject}")
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def governing_bump(package: str, version: str, repo_root: Path) -> str:
    """The bump class of the governing package — its ``version`` against its
    previous released tag, via :mod:`tai42_cli.api_gate`'s tag/version plumbing. A package
    with no previous tag is a first release and returns ``"major"`` (an unbounded
    first release carries any surface). When the package is not bumped on a train,
    its version is the last released one, so the bump reads as that last release's
    class — never major — and a consumer break correctly fails the gate."""
    previous = api_gate._previous_tag(package, version, repo_root)
    if previous is None:
        return "major"
    previous_version = previous[len(f"{package}-v") :]
    return api_gate._bump_class(previous_version, version)


def break_is_accepted(bump: str) -> bool:
    """A consumer boot break may ship only in a major bump."""
    return bump == "major"


# -------------------------------------------------------------- consumer specs


@dataclass(frozen=True)
class Consumer:
    """A consumer distribution to install and boot: its distribution name (used to
    read the installed descriptor), a display label, and the install argument uv
    receives (a wheel path or a requirement spec)."""

    dist_name: str
    label: str
    install_arg: str


def wheel_name_version(wheel: Path) -> tuple[str, str]:
    """The distribution name (hyphenated) and version parsed from a wheel filename."""
    match = _WHEEL_NAME_RE.match(wheel.name)
    if match is None:
        _fail(f"cannot parse a distribution name/version from wheel filename {wheel.name!r}")
    return match["name"].replace("_", "-"), match["version"]


def _req_dist_name(req: str) -> str:
    """The distribution name at the head of a requirement spec (``pkg==1.2`` ->
    ``pkg``), used to read the installed descriptor."""
    return re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0]


def collect_consumers(wheels: list[str], reqs: list[str]) -> list[Consumer]:
    """Resolve the supplied wheels and requirement specs into consumers. A wheel
    path that does not exist raises loudly — an upstream download that failed is a
    hard error, never a skipped consumer."""
    consumers: list[Consumer] = []
    for raw in wheels:
        wheel = Path(raw)
        if not wheel.is_file():
            _fail(f"consumer wheel not found at {wheel} — a failed download must not read as a pass")
        name, version = wheel_name_version(wheel)
        consumers.append(Consumer(dist_name=name, label=f"{name} {version}", install_arg=str(wheel.resolve())))
    for req in reqs:
        consumers.append(Consumer(dist_name=_req_dist_name(req), label=req, install_arg=req))
    return consumers


# --------------------------------------------------- first-party enumeration


_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def first_party_plugin_names(repo_root: Path) -> dict[str, str]:
    """Every packaged first-party plugin, mapping its distribution name to its member
    dir. A descriptor-only plugin dir (no ``pyproject.toml``) ships no distribution and
    is skipped."""
    import tomllib

    names: dict[str, str] = {}
    for pyproject in sorted(repo_root.glob("plugins/*/pyproject.toml")):
        name = tomllib.loads(pyproject.read_text())["project"]["name"]
        names[name] = str(pyproject.parent.relative_to(repo_root))
    return names


def release_bump_set(repo_root: Path) -> set[str]:
    """The distribution names being RELEASED in this train — a package whose
    release-please manifest version has no matching tag yet (a pending bump). Read the
    way the API gate reads versions: the manifest version against the package's tags.
    Booting such a package's stale published version is pointless (its new code is the
    candidate, its new version unpublished), so the caller excludes it."""
    import json

    manifest = json.loads((repo_root / ".release-please-manifest.json").read_text())
    config = json.loads((repo_root / "release-please-config.json").read_text())
    bumped: set[str] = set()
    for path, entry in config["packages"].items():
        package = entry.get("package-name")
        version = manifest.get(path)
        if not package or not version:
            continue
        tags = subprocess.run(
            ["git", "tag", "--list", f"{package}-v{version}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not tags:
            bumped.add(package)
    return bumped


def latest_pypi_version(name: str) -> str | None:
    """The highest final ``major.minor.patch`` release of ``name`` on PyPI, or ``None``
    when the distribution has no release yet (never published, or a 404)."""
    import urllib.error
    import urllib.request

    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            releases = json.loads(resp.read()).get("releases", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    finals = [v for v, files in releases.items() if files and _VERSION_RE.match(v)]
    if not finals:
        return None
    return max(finals, key=lambda v: tuple(int(part) for part in v.split(".")))


def enumerate_first_party(repo_root: Path) -> tuple[list[Consumer], list[str]]:
    """The first-party plugin consumers to boot — each NOT in this train's bump set, at
    its latest published PyPI version — plus a notice per plugin with no PyPI release yet
    (skipped, never a failure). One consumer per distribution."""
    bumped = release_bump_set(repo_root)
    consumers: list[Consumer] = []
    notices: list[str] = []
    for name in sorted(first_party_plugin_names(repo_root)):
        if name in bumped:
            continue
        version = latest_pypi_version(name)
        if version is None:
            notices.append(f"{name}: no PyPI release yet — skipped (not a failure)")
            continue
        consumers.append(Consumer(dist_name=name, label=f"{name} {version}", install_arg=f"{name}=={version}"))
    return consumers, notices


# ------------------------------------------------------------- plugin manifest


@dataclass
class Provides:
    """The manifest-relevant surface a plugin declares in its ``tai-plugin.yml``.

    Two families of surface. The ADDITIVE surface (routers/tools/channels/extensions/
    identity providers/lifecycle) mounts side by side without contending for an
    exclusive slot. The EXCLUSIVE single-module slots (``backend_module`` /
    ``storage_module`` / ``sandbox_module`` / ``monitoring_module``) and the
    multi-entry ``agents`` slot each select one provider for the whole app; a plugin
    is booted ALONE so two never contend. ``channels`` keeps each channel's name so
    its own Redis binding can be supplied (a channel refuses to register its doors
    without one). ``install_only`` lists the provide kinds whose provider cannot be
    exercised by a boot (its lifecycle needs a live external service CI has no
    stand-in for) — reported, never booted."""

    routers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    lifecycle: list[str] = field(default_factory=list)
    channels: list[tuple[str, str]] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    # Identity/accounts providers the plugin registers: the provider name is added
    # to the auth-provider chain so the accounts-provider-configured boot check
    # passes, and the module is mounted so the provider registers.
    providers: list[str] = field(default_factory=list)
    # The migration component a store-backed plugin declares: its binding is pinned
    # to the default database so its schema is applied and its boot-time schema gate
    # passes. ``None`` when the plugin ships no migrations.
    db_component: str | None = None
    # The exclusive single-module provider slots (one plugin selects one for the
    # whole app). ``None`` when the plugin declares no such slot.
    backend_module: str | None = None
    storage_module: str | None = None
    sandbox_module: str | None = None
    monitoring_module: str | None = None
    # The agent runtimes the plugin registers, each ``(name, module)`` — mounted as
    # one ``agents`` manifest entry per name so importing every module fires its
    # ``@agent`` decorator and registers.
    agents: list[tuple[str, str]] = field(default_factory=list)
    # Provide kinds the gate cannot mount into a boot (their provider's boot
    # lifecycle needs a live external service), each ``(kind, reason)`` — reported
    # ``install-only`` so the pass is never silent.
    install_only: list[tuple[str, str]] = field(default_factory=list)
    # The plugin's declared ``permissions.network``: it reaches an external endpoint.
    # A startup handler of such a plugin that fails ONLY by not reaching a configured
    # external endpoint (a connection-class error) is the plugin needing an external
    # service the gate cannot stand in for, not a candidate-core break — see
    # :func:`is_external_service_only`.
    network: bool = False

    def has_boot_surface(self) -> bool:
        """True when the descriptor declares a surface the gate can MOUNT into a
        boot. False means nothing loads the plugin's module — a core-only boot that
        proves nothing (the hollow pass this gate exists to prevent)."""
        return bool(
            self.routers
            or self.tools
            or self.channels
            or self.extensions
            or self.providers
            or self.lifecycle
            or self.backend_module
            or self.storage_module
            or self.sandbox_module
            or self.monitoring_module
            or self.agents
        )


# Provide kinds whose module registers a startup lifecycle (an inbound-signature
# verifier and kin) rather than a route/tool/channel — mounted so the plugin's
# registration runs, without selecting any exclusive provider slot.
_LIFECYCLE_KINDS = frozenset({"webhook-verifier"})

# Exclusive provider-slot kinds whose module the manifest selects by a single key,
# so mounting the plugin loads and registers it.
_SCALAR_SLOT_KEYS: dict[str, str] = {
    "backend": "backend_module",
    "storage": "storage_module",
    "sandbox": "sandbox_module",
    "monitoring": "monitoring_module",
}

# Provide kinds the gate installs but cannot boot: their provider's boot lifecycle
# needs a live external service CI has no stand-in for, mapped to the reason quoted
# in the ``install-only`` notice.
_INSTALL_ONLY_KINDS: dict[str, str] = {
    "config": (
        "a config provider is selected by TAI_CONFIG_MODE before the manifest loads, so it "
        "cannot be mounted through the boot manifest; a non-file provider also reads config "
        "from its live backing service (e.g. the Kubernetes API, in-cluster or via kubeconfig) "
        "at boot, which CI has no stand-in for"
    ),
}


def read_provides(plugin_yaml: dict) -> Provides:
    """The boot surface a plugin descriptor declares, additive and exclusive alike.

    Read by ``provides`` kind: ``router`` -> routers, ``tool`` -> tools, ``channel``
    -> channels (name + module), ``extension`` -> extensions, ``identity`` -> a
    registered provider (its name joins the auth-provider chain, its module is
    mounted), the lifecycle kinds -> lifecycle, the scalar-slot kinds
    (``backend``/``storage``/``sandbox``/``monitoring``) -> their single manifest slot,
    ``agent`` -> an agents entry (name + module); a top-level ``lifecycle_modules`` is
    appended too. A kind whose provider cannot be exercised by a boot
    (``config`` — see :data:`_INSTALL_ONLY_KINDS`) is recorded on ``install_only`` so
    it is reported, never mounted. One plugin is booted ALONE, so the exclusive slots
    it selects never contend with another consumer's."""
    provides = Provides()
    for entry in plugin_yaml.get("provides") or []:
        kind = entry.get("kind")
        if kind in _INSTALL_ONLY_KINDS and not any(k == kind for k, _ in provides.install_only):
            provides.install_only.append((kind, _INSTALL_ONLY_KINDS[kind]))
            continue
        module = entry.get("module")
        if module is None:
            continue
        if kind == "router":
            provides.routers.append(module)
        elif kind == "tool" and module not in provides.tools:
            provides.tools.append(module)
        elif kind == "channel":
            provides.channels.append((entry.get("name") or "", module))
        elif kind == "extension" and module not in provides.extensions:
            provides.extensions.append(module)
        elif kind == "identity" and entry.get("name"):
            provides.providers.append(entry["name"])
            provides.lifecycle.append(module)
        elif kind in _LIFECYCLE_KINDS:
            provides.lifecycle.append(module)
        elif kind in _SCALAR_SLOT_KEYS:
            # The scalar slot names the plugin's top-level PACKAGE, not the descriptor's
            # implementation submodule: the skeleton imports the whole named package
            # (registering the provider AND the tool/extension surface the package's
            # ``__init__`` pulls in from sibling modules) and whitelists every module
            # under that package as an explicitly-loaded plugin. Naming the impl
            # submodule would leave a sibling-registered tool unwhitelisted and abort boot.
            setattr(provides, _SCALAR_SLOT_KEYS[kind], module.split(".", 1)[0])
        elif kind == "agent" and entry.get("name"):
            provides.agents.append((entry["name"], module))
    for module in plugin_yaml.get("lifecycle_modules") or []:
        provides.lifecycle.append(module)
    if plugin_yaml.get("migrations"):
        provides.db_component = plugin_yaml.get("migrations_component") or plugin_yaml.get("package")
    permissions = plugin_yaml.get("permissions")
    provides.network = bool(isinstance(permissions, dict) and permissions.get("network"))
    return provides


def _db_binding_env(provides: Provides) -> dict[str, str]:
    """Pin a store-backed consumer's migration component to the default database so
    ``tai db migrate`` applies its chain and its boot-time schema gate passes (an
    unbound component is skipped by the migrator and then fails the gate at boot)."""
    if not provides.db_component:
        return {}
    slug = re.sub(r"[^A-Z0-9]", "_", provides.db_component.upper())
    return {f"TAI_DB_BINDING_{slug}": "default"}


def auth_providers(provides: Provides) -> list[str]:
    """The access-control auth-provider chain for a boot: the gate's own identity
    provider plus every provider the consumer registers (so the accounts-provider
    boot check passes for an accounts/identity plugin)."""
    return [_IDENTITY_PROVIDER_NAME, *dict.fromkeys(provides.providers)]


# The platform routers a served app always mounts, plus the access-control doors
# (api_keys/login) the on posture needs. Kept minimal and generic: the health and
# metrics probes, the tool/config surface, and the auth doors.
_CORE_ROUTERS = (
    "tai42_skeleton.routers.health",
    "tai42_skeleton.routers.metrics",
    "tai42_skeleton.routers.tools",
    "tai42_skeleton.routers.config",
    "tai42_skeleton.routers.api_keys",
    "tai42_skeleton.routers.login",
)


def build_manifest(provides: Provides) -> dict:
    """The serve manifest for one consumer: the core routers plus the consumer's
    declared surface — the additive modules (routers, tools, channels, extensions,
    lifecycle) AND the exclusive slots it selects (``backend_module`` /
    ``storage_module`` / ``sandbox_module`` / ``monitoring_module`` / ``agents``), so
    the plugin's module is imported and registers. The identity provider's lifecycle
    module is always present so access control resolves a provider."""
    lifecycle = [_IDENTITY_LIFECYCLE_MODULE, *provides.lifecycle]
    routers = [*_CORE_ROUTERS, *provides.routers]
    manifest: dict = {
        "default_routers": "none",
        "lifecycle_modules": lifecycle,
        "routers_modules": routers,
        "api_tools": {"enabled": False},
    }
    if provides.tools:
        manifest["tools"] = [{"title": module, "module": module} for module in provides.tools]
    if provides.channels:
        manifest["channel_modules"] = [module for _name, module in provides.channels]
    if provides.extensions:
        manifest["extensions_modules"] = provides.extensions
    if provides.backend_module:
        manifest["backend_module"] = provides.backend_module
    if provides.storage_module:
        manifest["storage_module"] = provides.storage_module
    if provides.sandbox_module:
        manifest["sandbox_module"] = provides.sandbox_module
    if provides.monitoring_module:
        manifest["monitoring_module"] = provides.monitoring_module
    if provides.agents:
        manifest["agents"] = [{"title": name, "module": module, "include": [name]} for name, module in provides.agents]
    return manifest


# The minimal placeholder config a consumer whose provider is BUILT EAGERLY at
# registration (a monitoring backend's ``register`` calls its factory, which reads
# its credentials at boot) needs to import and register. The values satisfy the
# provider's settings validation without reaching a live service — its client
# connects lazily on first use, never at boot — so health-ready proves the module
# imported and its registration ran. Keyed by distribution name; a provider that
# reads its settings lazily (storage/sandbox/backend register a class whose config
# is read at call time) needs no entry.
_BOOT_PLACEHOLDER_ENV: dict[str, dict[str, str]] = {
    "tai42-monitoring-langfuse": {
        "LANGFUSE_HOST": "http://127.0.0.1:1",
        "LANGFUSE_PUBLIC_KEY": "pk-boot-gate",
        "LANGFUSE_SECRET_KEY": "sk-boot-gate",
    },
}


def _slot_env(provides: Provides, dist_name: str, infra: Infra) -> dict[str, str]:
    """The env an exclusive-slot consumer's boot needs beyond the base: a registered
    task backend refuses to boot without the worker bus (the backend-runtime and
    server processes must converge on config reloads), so its queue Redis — the CI
    job's — is pinned; a provider built eagerly at registration gets its documented
    placeholder config."""
    env: dict[str, str] = {}
    if provides.backend_module:
        env["TAI_BUS_REDIS_URL"] = infra.redis_url
    env.update(_BOOT_PLACEHOLDER_ENV.get(dist_name, {}))
    return env


def _channel_env(provides: Provides, redis_url: str) -> dict[str, str]:
    """Each mounted channel binds its own Redis (``CHANNEL_<NAME>_REDIS_URL``) — a
    channel refuses to register its inbound doors without one, so a channel consumer
    could never reach the route-registration surface the gate exercises."""
    return {f"CHANNEL_{name.upper()}_REDIS_URL": redis_url for name, _module in provides.channels if name}


def _external_service_env(dist_name: str, blackhole_url: str) -> dict[str, str]:
    """The deployment config a network-declaring consumer needs to boot far enough to
    reach its external service, with every OUTBOUND endpoint pointed at ``blackhole_url``
    (a closed loopback port the gate allocates).

    A deployment supplies these credentials; the gate has no live messaging API or
    identity provider to point them at, so the endpoint is a black hole by design. The
    boot then exercises install -> import -> registration -> lifecycle up to the external
    boundary and surfaces a connection-class error there — the signal (see
    :func:`is_external_service_only`) that the plugin's startup needs an external service
    the gate cannot stand in for, distinct from a candidate-core break. This provisions
    config the same way :data:`_BOOT_PLACEHOLDER_ENV` and :func:`_slot_env` do; the
    install-only-vs-broken classification never keys on the distribution name, only on the
    declared network permission and the runtime error class. An OIDC issuer URL must be a
    loopback ``http`` origin or the discovery client refuses it before ever connecting, so
    the black hole is a loopback port.
    """
    import secrets

    if dist_name == "tai42-channel-telegram":
        return {
            "CHANNEL_TELEGRAM_BOT_TOKEN": "9900000000:consumer-boot-gate",
            "CHANNEL_TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(16),
            "CHANNEL_TELEGRAM_PUBLIC_BASE_URL": blackhole_url,
            "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT": "1",
            "CHANNEL_TELEGRAM_API_BASE_URL": blackhole_url,
        }
    if dist_name == "tai42-channel-slack":
        return {
            "CHANNEL_SLACK_BOT_USER_ID": "U0BOOTGATE",
            "CHANNEL_SLACK_BOT_TOKEN": "xoxb-consumer-boot-gate",
            "CHANNEL_SLACK_SIGNING_SECRET": secrets.token_hex(16),
            "CHANNEL_SLACK_API_BASE_URL": blackhole_url,
        }
    if dist_name == "tai42-identity-oidc":
        return {
            "TAI_IDENTITY_OIDC_ISSUER": blackhole_url,
            "TAI_IDENTITY_OIDC_AUDIENCE": "consumer-boot-gate",
        }
    if dist_name == "tai42-accounts-oidc":
        return {
            "TAI_ACCOUNTS_OIDC_STATE_KEY": secrets.token_hex(16),
            "TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL": blackhole_url,
            "TAI_ACCOUNTS_OIDC_PROVIDERS": json.dumps(
                [
                    {
                        "name": "boot",
                        "issuer": blackhole_url,
                        "client_id": "boot",
                        "client_secret": "boot",
                        "claim": "sub",
                    }
                ]
            ),
        }
    return {}


# ------------------------------------------------------------- failure parsing


# Substrings that identify a connection-class failure — the transport could not reach
# a host (a refused/timed-out/unresolved endpoint), as opposed to the endpoint answering
# with a rejection. These are the exception class names httpx/urllib raise and the
# transport-error wording the kit's discovery fetcher wraps them in. A boot whose only
# startup failures carry one of these, against the black-hole endpoint the gate configured
# for a network-declaring consumer, is the plugin needing an external service (never a
# candidate-core break, which surfaces as a missing symbol, a refused route, or a guard).
_CONNECTION_ERROR_MARKERS = (
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionRefusedError",
    "ReadTimeout",
    "ReadError",
    "PoolTimeout",
    "Transport error fetching",
    "Connection refused",
    "Connection reset",
    "All connection attempts failed",
    "Failed to establish a new connection",
    "Name or service not known",
    "Temporary failure in name resolution",
    "Network is unreachable",
    "No route to host",
    "[Errno 111]",
    "[Errno -2]",
    "[Errno -3]",
)


def _is_connection_error(text: str) -> bool:
    return any(marker in text for marker in _CONNECTION_ERROR_MARKERS)


@dataclass(frozen=True)
class BootFailure:
    """What a failed boot surfaced: the lifecycle handlers that raised, the per-handler
    error text, and the routes named in their errors, so the report says exactly what
    broke and the classification can read each handler's failure class."""

    handlers: tuple[str, ...]
    routes: tuple[str, ...]
    detail: str
    handler_errors: tuple[tuple[str, str], ...] = ()

    def summary(self) -> str:
        parts = []
        if self.routes:
            parts.append("route(s): " + ", ".join(self.routes))
        if self.handlers:
            parts.append("failing handler(s): " + ", ".join(self.handlers))
        return "; ".join(parts) if parts else self.detail


def parse_boot_failure(log_text: str) -> BootFailure:
    """The lifecycle handlers, their error text, and routes named in a boot's failure log.

    The skeleton raises ``lifecycle handlers failed: <name>: <Exc>(...), ...`` when a
    startup handler raises; each handler name, the exception text that follows it (sliced
    up to the next handler), and any ``METHOD /path`` tokens are extracted. When no
    structured line is present the last error line is kept as the detail, so a
    non-lifecycle boot failure still reports."""
    marker = "lifecycle handlers failed:"
    handlers: list[str] = []
    routes: list[str] = []
    handler_errors: list[tuple[str, str]] = []
    detail = ""
    idx = log_text.rfind(marker)
    if idx != -1:
        tail = log_text[idx + len(marker) :].splitlines()[0]
        detail = f"{marker}{tail}".strip()
        handlers = list(dict.fromkeys(m.group(1) for m in _HANDLER_RE.finditer(tail)))
        routes = list(dict.fromkeys(f"{m.group(1)} {m.group(2)}" for m in _ROUTE_RE.finditer(tail)))
        spans = list(_HANDLER_RE.finditer(tail))
        for pos, match in enumerate(spans):
            end = spans[pos + 1].start() if pos + 1 < len(spans) else len(tail)
            handler_errors.append((match.group(1), tail[match.start() : end]))
    if not detail:
        error_lines = [line.strip() for line in log_text.splitlines() if re.search(r"Error|Exception|Traceback", line)]
        detail = error_lines[-1] if error_lines else "boot failed with no error line captured"
    return BootFailure(
        handlers=tuple(handlers), routes=tuple(routes), detail=detail, handler_errors=tuple(handler_errors)
    )


def external_service_handlers(failure: BootFailure) -> tuple[str, ...]:
    """The failing startup handlers whose error is a connection-class failure reaching an
    external endpoint (de-duplicated, in first-seen order)."""
    return tuple(dict.fromkeys(name for name, text in failure.handler_errors if _is_connection_error(text)))


def is_external_service_only(failure: BootFailure) -> bool:
    """True when EVERY startup handler that failed did so with a connection-class error
    (and at least one did) — the boot got all the way to an external boundary and only the
    external round-trip, which the gate cannot complete, was unreachable. A single
    non-connection failure (a missing symbol, a refused route, a guard) makes this False,
    so a real candidate-core break is never reclassified."""
    return bool(failure.handler_errors) and all(_is_connection_error(text) for _name, text in failure.handler_errors)


# ---------------------------------------------------------------- boot runtime


@dataclass
class Infra:
    redis_url: str
    pg_host: str
    pg_port: str
    pg_user: str
    pg_password: str


def _infra_from_args(args: argparse.Namespace) -> Infra:
    return Infra(
        redis_url=args.redis_url,
        pg_host=args.pg_host,
        pg_port=str(args.pg_port),
        pg_user=args.pg_user,
        pg_password=args.pg_password,
    )


def _boot_env(
    config_dir: Path, manifest_path: Path, venv_bin: Path, db_name: str, infra: Infra, providers: list[str]
) -> dict[str, str]:
    """The child environment a served app boots under: the file config source, one
    Postgres database serving every component (each component binds to ``default``),
    the shared Redis, and access control on with the ``providers`` chain selected."""
    return {
        "PATH": os.pathsep.join([str(venv_bin), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", str(config_dir)),
        "TAI_CONFIG_MODE": "file",
        "TAI_CONFIG_DIR_PATH": str(config_dir),
        "TAI_MANIFEST_PATH": str(manifest_path),
        # Point the plugin-prefix scan at the boot venv so ``tai db migrate`` discovers
        # every installed consumer's migration chain (the discovery scans a prefix's
        # site dirs, and the venv is exactly such a prefix); without it a pip-installed
        # store-backed consumer's schema stays pending and its boot gate refuses.
        "TAI_PLUGINS_PREFIX": str(venv_bin.parent),
        "TAI_DATABASE_DEFAULT_PG_HOST": infra.pg_host,
        "TAI_DATABASE_DEFAULT_PG_PORT": infra.pg_port,
        "TAI_DATABASE_DEFAULT_PG_USER": infra.pg_user,
        "TAI_DATABASE_DEFAULT_PG_PASSWORD": infra.pg_password,
        "TAI_DATABASE_DEFAULT_PG_DB": db_name,
        "TAI_DEFAULT_REDIS_URL": infra.redis_url,
        "ACCESS_CONTROL_ENABLE": "true",
        "ACCESS_CONTROL_AUTH_PROVIDERS": json.dumps(providers),
        "ACCESS_CONTROL_REDIS_URL": infra.redis_url,
        "INTERACTIONS_REDIS_URL": infra.redis_url,
        "TAI_TOOL_RUNS_REDIS_URL": infra.redis_url,
        "HOOKS_REDIS_URL": infra.redis_url,
        "SUB_MCP_REDIS_URL": infra.redis_url,
        "CONNECTOR_STORE_REDIS_URL": infra.redis_url,
        "TAI_RATE_LIMIT_REDIS_URL": infra.redis_url,
    }


def _run_venv_py(venv_bin: Path, code: str, payload: dict) -> subprocess.CompletedProcess[str]:
    """Run a short Python snippet in the boot venv (which carries the Postgres and
    Redis clients), passing ``payload`` as a JSON argv. The gate's own runtime env
    therefore needs neither client — the infra writes ride the same interpreter the
    app boots under."""
    return subprocess.run([str(venv_bin / "python"), "-c", code, json.dumps(payload)], capture_output=True, text=True)


def _create_database(venv_bin: Path, infra: Infra) -> str:
    """A fresh, empty database for one boot, so the migration chain and the
    access-control seed always start clean and boots never collide."""
    code = (
        "import json,sys,secrets,psycopg\n"
        "p=json.loads(sys.argv[1])\n"
        "name='consumer_boot_'+secrets.token_hex(6)\n"
        "with psycopg.connect(host=p['h'],port=p['port'],user=p['u'],password=p['pw'],"
        "dbname='postgres',autocommit=True) as c:\n"
        "    c.execute('CREATE DATABASE \"'+name+'\"')\n"
        "sys.stdout.write(name)\n"
    )
    result = _run_venv_py(venv_bin, code, _pg_payload(infra))
    if result.returncode != 0 or not result.stdout.strip():
        _fail(f"could not create a fresh boot database: {result.stderr.strip()[-400:]}")
    return result.stdout.strip()


def _seed_access_control(venv_bin: Path, db_name: str, infra: Infra) -> None:
    """Seed the minimum access-control state a health probe needs to answer 200:
    a root key in the identity provider's store and a policy row, plus a route
    table pinning the readiness probes public and mapping every other path to a
    scope the root satisfies. Without it, access control answers /health 403 and
    readiness could never confirm a healthy boot."""
    code = (
        "import json,sys,secrets,hashlib,psycopg,redis\n"
        "p=json.loads(sys.argv[1])\n"
        "hashed=hashlib.sha256(('sk-'+secrets.token_urlsafe(24)).encode()).hexdigest()\n"
        "rc=redis.Redis.from_url(p['redis'],decode_responses=True)\n"
        "try:\n"
        "    rc.hset('ac:key:'+hashed,mapping={'user_id':'root','description':'boot-gate'})\n"
        "    rc.set('ac:management:key:root',hashed)\n"
        "finally:\n"
        "    rc.close()\n"
        "with psycopg.connect(host=p['h'],port=p['port'],user=p['u'],password=p['pw'],dbname=p['db']) as conn:\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute('INSERT INTO access_control_policies (user_id, scopes) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET scopes = EXCLUDED.scopes',('root',['*']))\n"
        "        cur.executemany('INSERT INTO access_control_routes (url, scope_id, pattern) VALUES (%s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET scope_id = EXCLUDED.scope_id, pattern = EXCLUDED.pattern',"
        "[('/health','public',None),('/metrics','public',None),"
        "('all-routes','all',r'^/(?!health$)(?!metrics$).*$')])\n"
        "    conn.commit()\n"
    )
    payload = {**_pg_payload(infra), "db": db_name, "redis": infra.redis_url}
    result = _run_venv_py(venv_bin, code, payload)
    if result.returncode != 0:
        _fail(f"could not seed access control for the boot: {result.stderr.strip()[-400:]}")


def _pg_payload(infra: Infra) -> dict:
    return {"h": infra.pg_host, "port": infra.pg_port, "u": infra.pg_user, "pw": infra.pg_password}


def _allocate_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def boot_consumer(
    consumer: Consumer, venv_bin: Path, provides: Provides, workdir: Path, infra: Infra
) -> BootFailure | None:
    """Boot one consumer against the candidate core and wait for health. Returns
    ``None`` on a healthy boot, or the parsed :class:`BootFailure` when the serve
    process exits before health or never becomes ready."""
    import signal
    import time
    import urllib.error
    import urllib.request

    import yaml

    tai = venv_bin / "tai"
    config_dir = workdir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = config_dir / "manifest.yml"
    manifest_path.write_text(yaml.safe_dump(build_manifest(provides), sort_keys=False))

    db_name = _create_database(venv_bin, infra)
    blackhole_url = f"http://127.0.0.1:{_allocate_port()}"
    env = {
        **_boot_env(config_dir, manifest_path, venv_bin, db_name, infra, auth_providers(provides)),
        **_channel_env(provides, infra.redis_url),
        **_db_binding_env(provides),
        **_slot_env(provides, consumer.dist_name, infra),
        **(_external_service_env(consumer.dist_name, blackhole_url) if provides.network else {}),
    }

    migrate = subprocess.run([str(tai), "db", "migrate"], env=env, capture_output=True, text=True)
    if migrate.returncode != 0:
        return BootFailure(handlers=(), routes=(), detail=f"tai db migrate failed: {migrate.stderr.strip()[-500:]}")
    _seed_access_control(venv_bin, db_name, infra)

    port = _allocate_port()
    log_path = workdir / "serve.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            [
                str(tai),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--manifest-path",
                str(manifest_path),
            ],
            env=env,
            cwd=str(config_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    health_url = f"http://127.0.0.1:{port}/health"
    ready = False
    deadline = time.monotonic() + _HEALTH_DEADLINE_S
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(health_url, timeout=3) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(1.0)
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    if ready:
        return None
    return parse_boot_failure(log_path.read_text())


# uv prints this exact line when a consumer's declared dependency range excludes the
# candidate core, so the boot venv cannot be assembled — the most explicit form of a
# consumer break, classified by the governing bump like a boot failure.
_NO_SOLUTION_MARKER = "No solution found when resolving dependencies"


class _ResolutionConflict(Exception):
    """The boot venv install failed because uv found no dependency solution: a consumer's
    requirement range excludes the candidate core. Carries the resolver output so the
    caller classifies it by the governing bump."""

    def __init__(self, resolver_stderr: str) -> None:
        super().__init__(resolver_stderr)
        self.resolver_stderr = resolver_stderr


def _is_resolution_conflict(install_stderr: str) -> bool:
    """True when an install failure is uv reporting no dependency solution (a consumer
    range excluding the candidate), not a network, build or bad-wheel failure."""
    return _NO_SOLUTION_MARKER in install_stderr


def _resolver_conclusion(resolver_stderr: str) -> str:
    """The resolver's own conclusion, from the ``No solution found`` line onward, quoted
    verbatim in the accepted-break notice."""
    idx = resolver_stderr.find(_NO_SOLUTION_MARKER)
    return resolver_stderr[idx:].strip() if idx != -1 else resolver_stderr.strip()


def _install_venv(
    repo_root: Path, core_dirs: list[str], identity_package: str, consumers: list[Consumer], venv: Path
) -> Path:
    """Create the boot venv and install the editable candidate core, the identity
    provider, and every consumer into it. Returns the venv's ``bin`` dir. A no-solution
    resolution failure raises :class:`_ResolutionConflict` for the caller to classify by
    the bump; any other install failure is a hard failure here."""
    subprocess.run(["uv", "venv", "--python", "3.13", str(venv)], cwd=repo_root, check=True, capture_output=True)
    venv_bin = venv / "bin"
    install_args = ["uv", "pip", "install", "--python", str(venv_bin / "python")]
    for core_dir in core_dirs:
        extras = f"[{_KIT_EXTRAS}]" if core_dir.endswith("/kit") else ""
        install_args += ["-e", f"{core_dir}{extras}"]
    install_args.append(identity_package)
    install_args += [consumer.install_arg for consumer in consumers]
    result = subprocess.run(install_args, cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()[-800:]
        if _is_resolution_conflict(stderr):
            raise _ResolutionConflict(stderr)
        _fail(f"boot venv install failed: {stderr}")
    return venv_bin


def _report_unresolvable_consumers(header: str, bump: str, consumers: list[Consumer], resolver_stderr: str) -> None:
    """Classify a boot-venv resolution conflict by the governing bump: under a major bump
    report every supplied consumer as an accepted break (a notice quoting the resolver's
    conclusion, the gate passes); otherwise fail with the resolver output, as any
    unresolved install does."""
    if not break_is_accepted(bump):
        _fail(f"boot venv install failed: {resolver_stderr}")
    names = ", ".join(consumer.label for consumer in consumers)
    print(
        f"::notice::{header}: {len(consumers)} consumer(s) cannot resolve against the candidate, "
        f"ACCEPTED as a major-bump break: {names}"
    )
    print(f"  - {_resolver_conclusion(resolver_stderr)}")
    print(f"{header}: accepted break under a major bump ({len(consumers)} unresolvable) — gate passes.")


def _load_plugin_yaml(venv_bin: Path, dist_name: str) -> dict:
    """Read the named consumer distribution's ``tai-plugin.yml`` from the boot venv —
    the descriptor the deployment actually ships, not a tree copy. Located through the
    distribution's own file manifest (never by importing its package: a plugin whose
    ``__init__`` registers against ``tai42_app`` at import raises before the app binds,
    which is exactly the plugins this gate must read), so a co-installed plugin's
    descriptor is never mistaken for it. A consumer that ships no descriptor cannot
    declare a boot surface to exercise; the caller treats that as a hard error, never a
    silent skip."""
    script = (
        "import importlib.metadata as m, sys\n"
        f"dist = m.distribution({dist_name!r})\n"
        "found = ''\n"
        "for f in dist.files or []:\n"
        "    if f.name == 'tai-plugin.yml':\n"
        "        found = dist.locate_file(f).read_text()\n"
        "        break\n"
        "sys.stdout.write(found)\n"
    )
    result = subprocess.run([str(venv_bin / "python"), "-c", script], capture_output=True, text=True)
    if result.returncode != 0:
        _fail(f"could not read the {dist_name} descriptor from the boot venv: {result.stderr.strip()[-400:]}")
    if not result.stdout.strip():
        return {}
    import yaml

    return yaml.safe_load(result.stdout) or {}


def _resolve_version(args: argparse.Namespace, repo_root: Path) -> str:
    if args.version is not None:
        return args.version
    return read_project_version(repo_root / args.dir)


def _emit_matrix(repo_root: Path) -> None:
    """Print a GitHub-Actions matrix of the first-party consumers to boot (one entry
    per distribution, each ``{label, req}``) as ``{"include": [...]}`` on stdout, and a
    ``::notice::`` per plugin skipped for having no PyPI release yet. An empty include
    is valid — the matrix job then has no combinations and is skipped."""
    consumers, notices = enumerate_first_party(repo_root)
    for notice in notices:
        print(f"::notice::consumer-boot-gate: {notice}", file=sys.stderr)
    include = [{"label": c.label, "req": c.install_arg} for c in consumers]
    print(json.dumps({"include": include}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", help="governing release-please package-name (the bump owner)")
    parser.add_argument("--dir", help="the governing package's member dir")
    parser.add_argument("--version", help="governing version (default: read from the member's pyproject.toml)")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--core-dir", action="append", dest="core_dirs", help="editable core member dir (repeatable)")
    parser.add_argument("--consumer-wheel", action="append", default=[], help="path to a consumer wheel (repeatable)")
    parser.add_argument("--consumer-req", action="append", default=[], help="a consumer requirement spec (repeatable)")
    parser.add_argument(
        "--emit-matrix",
        action="store_true",
        help="enumerate first-party consumers (minus the bump set) and print a CI matrix, then exit",
    )
    parser.add_argument("--identity-package", default=_DEFAULT_IDENTITY_PACKAGE)
    parser.add_argument("--redis-url", default=os.environ.get("CONSUMER_BOOT_REDIS_URL", "redis://127.0.0.1:6379"))
    parser.add_argument("--pg-host", default=os.environ.get("CONSUMER_BOOT_PG_HOST", "127.0.0.1"))
    parser.add_argument("--pg-port", default=os.environ.get("CONSUMER_BOOT_PG_PORT", "5432"))
    parser.add_argument("--pg-user", default=os.environ.get("CONSUMER_BOOT_PG_USER", "postgres"))
    parser.add_argument("--pg-password", default=os.environ.get("CONSUMER_BOOT_PG_PASSWORD", "postgres"))
    parser.add_argument("--workdir", type=Path, help="scratch dir for the venv, config and logs (default: a tempdir)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch dir after the run")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    if args.emit_matrix:
        _emit_matrix(repo_root)
        return
    if not args.package or not args.dir:
        _fail("--package and --dir are required to boot a consumer (omit them only with --emit-matrix)")
    core_dirs = args.core_dirs or list(_DEFAULT_CORE_DIRS)
    version = _resolve_version(args, repo_root)
    bump = governing_bump(args.package, version, repo_root)

    consumers = collect_consumers(args.consumer_wheel, args.consumer_req)
    header = f"consumer-boot-gate: {args.package} {version} ({bump} bump)"
    if not consumers:
        print(f"{header}: no previously published consumer supplied — nothing to boot, gate passes.")
        return

    workdir = args.workdir.resolve() if args.workdir else Path(tempfile.mkdtemp(prefix="consumer-boot-"))
    workdir.mkdir(parents=True, exist_ok=True)
    infra = _infra_from_args(args)
    try:
        venv_bin = _install_venv(repo_root, core_dirs, args.identity_package, consumers, workdir / "venv")
    except _ResolutionConflict as conflict:
        _report_unresolvable_consumers(header, bump, consumers, conflict.resolver_stderr)
        return

    print(f"{header}: booting {len(consumers)} consumer(s) against the candidate core.")
    booted = 0
    install_only = 0
    external_only = 0
    failures: list[tuple[Consumer, BootFailure]] = []
    for index, consumer in enumerate(consumers):
        plugin_yaml = _load_plugin_yaml(venv_bin, consumer.dist_name)
        if not plugin_yaml:
            _fail(
                f"{consumer.label}: ships no tai-plugin.yml descriptor, so no boot surface can be mounted — "
                f"the gate cannot prove it boots. A supplied consumer must be a plugin distribution."
            )
        provides = read_provides(plugin_yaml)
        for kind, reason in provides.install_only:
            print(f"::notice::{consumer.label}: {kind} provider install-only: {reason}")
        if not provides.has_boot_surface():
            if provides.install_only:
                # Installed against the candidate core (its dependency closure resolves)
                # but not bootable: reported, never a silent pass.
                print(f"  - {consumer.label}: install-only (installs; not booted).")
                install_only += 1
                continue
            _fail(
                f"{consumer.label}: declares no surface the gate can mount and no install-only provider — "
                f"a core-only boot would prove nothing (the hollow pass this gate exists to prevent). "
                f"Its tai-plugin.yml 'provides' names no router/tool/channel/extension/identity/lifecycle "
                f"module, no backend/storage/sandbox/monitoring/agent slot, and no install-only kind."
            )
        boot_dir = workdir / f"boot-{index}"
        boot_dir.mkdir(parents=True, exist_ok=True)
        failure = boot_consumer(consumer, venv_bin, provides, boot_dir, infra)
        if failure is None:
            print(f"  - {consumer.label}: booted, health ready.")
            booted += 1
        elif provides.network and is_external_service_only(failure):
            # The boot ran through install, import, registration and lifecycle and then
            # could not reach the external endpoint the gate black-holed for it — the
            # plugin's startup needs an external service the gate cannot stand in for, not
            # a candidate-core break. Reported, never a silent pass and never a failure.
            reached = ", ".join(external_service_handlers(failure))
            print(f"  - {consumer.label}: install-only (external service: {reached}).")
            external_only += 1
        else:
            print(f"  - {consumer.label}: BOOT FAILED — {failure.summary()}")
            failures.append((consumer, failure))

    if not args.keep and args.workdir is None:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    tally = f"{booted} booted, {install_only} install-only, {external_only} install-only (external service)"
    if not failures:
        print(f"{header}: every supplied consumer accounted for ({tally}) — gate passes.")
        return

    lines = [f"{consumer.label}: {failure.summary()}" for consumer, failure in failures]
    if break_is_accepted(bump):
        print(f"::notice::{header}: {len(failures)} consumer(s) fail to boot, ACCEPTED as a major-bump break:")
        for line in lines:
            print(f"  - {line}")
        print(f"{header}: accepted break under a major bump ({tally}, {len(failures)} broken) — gate passes.")
        return
    _fail(
        f"{header}: {len(failures)} previously published consumer(s) fail to boot against the candidate, "
        f"which is a breaking change not carried by a major bump ({tally}). Offending: {'; '.join(lines)}"
    )


if __name__ == "__main__":
    main()

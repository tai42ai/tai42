"""The manifest-relevant surface a plugin descriptor declares, and the reader
that parses a ``tai-plugin.yml`` ``provides`` block into it."""

from __future__ import annotations

from dataclasses import dataclass, field


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


def _add_router(provides: Provides, entry: dict, module: str) -> None:
    provides.routers.append(module)


def _add_tool(provides: Provides, entry: dict, module: str) -> None:
    if module not in provides.tools:
        provides.tools.append(module)


def _add_channel(provides: Provides, entry: dict, module: str) -> None:
    provides.channels.append((entry.get("name") or "", module))


def _add_extension(provides: Provides, entry: dict, module: str) -> None:
    if module not in provides.extensions:
        provides.extensions.append(module)


def _add_identity(provides: Provides, entry: dict, module: str) -> None:
    if entry.get("name"):
        provides.providers.append(entry["name"])
        provides.lifecycle.append(module)


def _add_agent(provides: Provides, entry: dict, module: str) -> None:
    if entry.get("name"):
        provides.agents.append((entry["name"], module))


# The per-``provides``-kind handler for each ADDITIVE surface, each mounting ``module``
# (and reading ``entry`` for a name where the kind carries one). The lifecycle and
# scalar-slot kinds are dispatched separately (they key off frozenset / dict membership).
_ADDITIVE_HANDLERS = {
    "router": _add_router,
    "tool": _add_tool,
    "channel": _add_channel,
    "extension": _add_extension,
    "identity": _add_identity,
    "agent": _add_agent,
}


def _read_top_level(provides: Provides, plugin_yaml: dict) -> None:
    """Fold the descriptor's top-level (non-``provides``) surface into ``provides``:
    the extra ``lifecycle_modules``, the migration component, and the network permission."""
    for module in plugin_yaml.get("lifecycle_modules") or []:
        provides.lifecycle.append(module)
    if plugin_yaml.get("migrations"):
        provides.db_component = plugin_yaml.get("migrations_component") or plugin_yaml.get("package")
    permissions = plugin_yaml.get("permissions")
    provides.network = bool(isinstance(permissions, dict) and permissions.get("network"))


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
        handler = _ADDITIVE_HANDLERS.get(kind)
        if handler is not None:
            handler(provides, entry, module)
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
    _read_top_level(provides, plugin_yaml)
    return provides

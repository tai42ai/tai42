"""Manifest contract: ``Manifest`` + the ``*Config`` models (incl. the transport
config ``TaiMCPConfig``) + the filter-predicate signatures.

The impl methods that build the derived lookup maps and run the include/exclude
filtering (``model_post_init`` + the private ``_build_*``/``_should_include``
helpers) are logic and stay with the impl — only the field shape, the model
validators, and the public predicate/query signatures are the contract.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.connectors.models import ConnectorRef
from tai42_contract.connectors.providers import ProviderDescriptor

# One element inside an extension combo. A bare ``str`` names an extension with no
# author-bound config; a ``{"name": str, "config": dict}`` mapping binds
# per-application config to that extension, threaded to its factory at build time
# (e.g. ``ask_external``'s author-bound ``verifier``). The config is NEVER an
# LLM-facing tool param.
ExtensionElement = str | dict[str, Any]


class BaseConfig(BaseModel):
    title: str
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class MCPConfig(BaseModel):
    type: str | None = None
    url: str | None = None
    uds: str | None = None
    command: str | None = None
    # A validator guarantees non-None for these three (None normalizes to the
    # empty container), so the annotations drop ``| None`` and stay honest.
    args: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("url", "uds", "command", mode="before")
    @classmethod
    def _empty_transport_to_none(cls, value: object) -> object:
        """Normalize an empty transport string to ``None`` (mirrors
        ``McpServerDescriptor._check_url``) so the transport gate and the
        ``is_*`` predicates agree that an empty value is "not set"."""
        if value == "":
            return None
        return value

    @field_validator("args", mode="before")
    @classmethod
    def normalize_args(cls, value: object) -> list[str]:
        if value is None:
            return []
        return cast("list[str]", value)

    @field_validator("headers", "env", mode="before")
    @classmethod
    def normalize_dict_values(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"Expected a dictionary, got {type(value).__name__}")

        # Keys pass through as-is; pydantic validates them against the field's
        # dict[str, str] annotation after this hook. A None value is a malformed
        # entry and fails loud (naming the key) rather than being dropped.
        items = cast("dict[str, object]", value)
        result: dict[str, str] = {}
        for key, item in items.items():
            if item is None:
                raise ValueError(f"value for key {key!r} must not be None")
            result[key] = str(item)
        return result

    @model_validator(mode="after")
    def _exactly_one_transport(self):
        """Reject manifests that set more than one transport field at once.

        ``url`` / ``uds`` / ``command`` are mutually exclusive — the downstream
        MCP adapter dispatches on whichever is set first, so an entry with both
        ``url`` and ``command`` would behave inconsistently depending on
        iteration order at the call site. Zero-transport is intentionally
        permitted at construction (tests/partial fixtures build an empty
        MCPConfig then mutate it); the missing-transport case is caught later at
        call time by the outbound MCP adapter.

        ``args`` / ``env`` are launcher-only (require ``command``); ``headers`` is
        HTTP-only (requires ``url``). The check is permissive: empty containers
        count as "not set" — only a NON-EMPTY mismatched auxiliary field rejects.
        """
        configured = [
            name
            for name, value in (
                ("url", self.url),
                ("uds", self.uds),
                ("command", self.command),
            )
            if value is not None
        ]
        if len(configured) > 1:
            raise ValueError(f"MCPConfig: exactly one of url/uds/command may be set; got {configured!r}")

        if configured:
            transport = configured[0]
            if transport != "command":
                if self.args:
                    raise ValueError(
                        f"MCPConfig: ``args`` is launcher-only and "
                        f"requires ``command``; got transport={transport!r} "
                        f"with args={self.args!r}",
                    )
                if self.env:
                    raise ValueError(
                        f"MCPConfig: ``env`` is launcher-only and "
                        f"requires ``command``; got transport={transport!r} "
                        f"with env={list(self.env.keys())!r}",
                    )
            if transport != "url" and self.headers:
                raise ValueError(
                    f"MCPConfig: ``headers`` is HTTP-only and "
                    f"requires ``url``; got transport={transport!r} "
                    f"with headers={list(self.headers.keys())!r}",
                )

        return self


def _normalize_extension_element(key: str, member: object) -> ExtensionElement:
    """Validate one combo member and return it in canonical form.

    A ``str`` is an extension name with no bound config and passes through. A
    ``dict`` binds author config and must carry a non-empty string ``name`` (an
    extension-name shape — registration is checked later) and a ``config``
    mapping, with no other keys; any deviation raises loudly.
    """
    if isinstance(member, str):
        return member
    if isinstance(member, dict):
        entry = cast("dict[str, object]", member)
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"extensions[{key!r}] element must have a non-empty string 'name'")
        config = entry.get("config")
        if not isinstance(config, dict):
            raise ValueError(f"extensions[{key!r}] element {name!r} must carry a 'config' mapping")
        extra = set(entry) - {"name", "config"}
        if extra:
            raise ValueError(f"extensions[{key!r}] element {name!r} has unexpected keys: {sorted(extra)!r}")
        return {"name": name, "config": dict(cast("dict[str, object]", config))}
    raise ValueError(f"extensions[{key!r}] combo members must be an extension name or a {{'name', 'config'}} mapping")


class ExtensionsConfigMixin(BaseModel):
    """Carries the ``extensions`` attachment map, mixed into the config kinds
    that support clip-on extensions (``ToolsConfig`` and ``TaiMCPConfig``).

    ``extensions`` maps a tool base-name to the extension combo(s) attached to
    it, applied AFTER selection and independent of ``include``/``exclude``. The
    stored shape is always ``tool -> list-of-combos``
    (``dict[str, list[list[ExtensionElement]]]``) so resolution reads ONE shape
    regardless of how the YAML was written; each combo element is an extension
    name or a ``{"name", "config"}`` mapping binding author config to it. The
    normalizing validator wraps a flat combo and rejects malformed input loudly.
    """

    extensions: dict[str, list[list[ExtensionElement]]] = Field(default_factory=dict)

    @field_validator("extensions", mode="before")
    @classmethod
    def normalize_extensions(cls, value: object) -> dict[str, list[list[ExtensionElement]]]:
        """Normalize each value to a list of combos and reject malformed input
        loudly (no silent coercion, mirroring ``normalize_dict_values`` above).

        A flat combo value is a single combo and is wrapped
        (``{weather: [chain]}`` -> ``{"weather": [["chain"]]}``); a list-of-combos
        value is kept as multiple combos. A combo element is an extension name or
        a ``{"name", "config"}`` mapping (author-bound config). This enforces
        SHAPE only: it does NOT check that the extension names are registered
        (``ExtensionRegistry``'s job) or that the tool is selected
        (``missing_tools``' job).
        """
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"extensions must be a mapping, got {type(value).__name__}")

        items = cast("dict[str, object]", value)
        result: dict[str, list[list[ExtensionElement]]] = {}
        for key, combos in items.items():
            if not isinstance(combos, list):
                raise ValueError(f"extensions[{key!r}] must be a list of names or a list of combos")
            if not combos:
                raise ValueError(f"extensions[{key!r}] must not be empty; omit the key instead of attaching nothing")

            entries = cast("list[object]", combos)
            # A combo member is a list; an extension element is a str or a
            # {"name", "config"} mapping. A flat combo mixes only elements; a
            # list-of-combos holds only lists — a mix of the two is malformed.
            all_element = all(isinstance(member, (str, dict)) for member in entries)
            all_list = all(isinstance(member, list) for member in entries)
            if all_element:
                result[key] = [[_normalize_extension_element(key, member) for member in entries]]
            elif all_list:
                normalized: list[list[ExtensionElement]] = []
                for combo in cast("list[list[object]]", entries):
                    if not combo:
                        raise ValueError(
                            f"extensions[{key!r}] combo must not be empty; a combo names at least one extension"
                        )
                    normalized.append([_normalize_extension_element(key, member) for member in combo])
                result[key] = normalized
            else:
                raise ValueError(
                    f"extensions[{key!r}] must be a flat combo of extension elements or a list of combos, "
                    "not a mix of the two"
                )
        return result


class ToolsConfig(BaseConfig, ExtensionsConfigMixin):
    module: str


class AgentsConfig(BaseConfig):
    # ``extra="forbid"``: an ``extensions`` key on an agents config is a
    # misconfig (agents carry no clip-on extensions) — reject it loudly as a
    # pydantic extra-field error rather than silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    module: str


class TaiMCPConfig(BaseConfig, ExtensionsConfigMixin):
    config: MCPConfig
    managed: ConnectorRef | None = None

    @property
    def is_managed(self) -> bool:
        return self.managed is not None


class ApiToolsConfig(ExtensionsConfigMixin):
    """Curates the MCP tool surface projected from the operations layer.

    A single app-owned block, so it carries no ``title`` and is deliberately NOT
    a ``BaseConfig`` subclass. ``include``/``exclude`` name operations to project
    or suppress; ``expose_destructive`` gates destructive-flagged operations; the
    ``extensions`` map (from the mixin) attaches extension combos to projected
    operations. Unlike ``BaseConfig``, a name appearing in BOTH ``include`` and
    ``exclude`` is a loud validation error — this stricter rule is unique to this
    config and is never retrofitted onto other configs.
    """

    enabled: bool = True
    expose_destructive: bool = True
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_include_exclude_overlap(self):
        overlap = sorted(set(self.include) & set(self.exclude))
        if overlap:
            raise ValueError(
                f"ApiToolsConfig: operation(s) {overlap!r} appear in both include and exclude; "
                "an entry may be in one list only"
            )
        return self


class Manifest(BaseModel):
    # ``extra="forbid"``: an unknown top-level manifest key is a misconfig — fail
    # loudly naming the key rather than silently dropping it.
    model_config = ConfigDict(extra="forbid")

    # Non-optional: default_factory guarantees a list; an explicit null is
    # rejected at validation instead of crashing the map build.
    tools: list[ToolsConfig] = Field(default_factory=list[ToolsConfig])
    agents: list[AgentsConfig] = Field(default_factory=list[AgentsConfig])
    mcp: list[TaiMCPConfig] = Field(default_factory=list[TaiMCPConfig])

    # Non-optional (matching tools/agents/mcp above): default_factory guarantees
    # a list and an explicit null is rejected at validation, never accepted.
    middlewares_modules: list[str] = Field(default_factory=list)
    routers_modules: list[str] = Field(default_factory=list)

    # Selects the skeleton's DEFAULT router set; ``routers_modules`` always lists
    # EXTRAS mounted on top of the selected default set:
    #   - ``"all"`` — mount the core API routers plus the Studio SPA catch-all,
    #     served last (the full browser-UI deployment).
    #   - ``"api"`` — mount the core API routers only, with no SPA serving (a
    #     headless JSON HTTP API).
    #   - ``"none"`` — mount no defaults; ``routers_modules`` lists every router
    #     explicitly (a fully manual or MCP-only surface).
    # A NORMAL serialized field (no exclude): it must survive ``model_dump`` so it
    # appears in ``live_manifest`` output.
    default_routers: Literal["all", "api", "none"] = "all"

    extensions_modules: list[str] = Field(default_factory=list)
    lifecycle_modules: list[str] = Field(default_factory=list)

    # Declarative connector providers registered at boot/reload through the
    # ``tai42_app.connectors.register_connector`` facet (a connector is pure
    # data — it carries no import path). A NORMAL serialized field (no exclude):
    # it must survive ``model_dump`` so it appears in ``live_manifest`` output
    # and rides the deep copy the reload seam re-registers from.
    connectors: list[ProviderDescriptor] = Field(default_factory=list[ProviderDescriptor])

    # Modules imported at app load whose import side-effect registers webhook
    # verifiers via ``tai42_app.webhook_verifiers.register(...)`` — the dedicated
    # home for verifier plugins (generic boot imports stay in
    # ``lifecycle_modules``).
    webhook_verifier_modules: list[str] = Field(default_factory=list)

    # Modules imported at app load whose import side-effect registers channels
    # via ``tai42_app.channels.register(...)`` AND registers each channel's
    # inbound HTTP route.
    channel_modules: list[str] = Field(default_factory=list)

    backend_module: str | None = None
    storage_module: str | None = None
    monitoring_module: str | None = None
    static_dir: str | None = None

    # Names the installed Python packages that ship a Studio plugin. A discovery
    # list like ``routers_modules``, but naming packages rather than import paths.
    # A NORMAL serialized field (no exclude): it must survive ``model_dump`` so it
    # appears in ``live_manifest`` output for the Studio-plugin registry to read.
    studio_plugins: list[str] = Field(default_factory=list)

    # Curates which built-in tools the end user is offered in the authoring
    # surfaces. A NORMAL serialized field (no exclude): it must survive
    # ``model_dump`` so it appears in ``live_manifest`` output.
    user_tools: set[str] = Field(default_factory=set)

    # Curates the MCP tool surface projected from the operations layer. A NORMAL
    # serialized field (no exclude): it must survive ``model_dump`` so it appears
    # in ``live_manifest`` output.
    api_tools: ApiToolsConfig = Field(default_factory=ApiToolsConfig)

    tools_list: set[str] | None = Field(default_factory=set, exclude=True)

    # Derived attachment map (tool base-name -> extension combos), unioned from
    # the ``extensions`` maps of ``tools`` + ``mcp`` after selection. The impl
    # builds it non-None in ``model_post_init``, mirroring ``tools_list``.
    tool_extensions: dict[str, list[list[ExtensionElement]]] | None = Field(default_factory=dict, exclude=True)

    tools_map: dict[str, ToolsConfig] | None = Field(default_factory=dict, exclude=True)
    agents_map: dict[str, AgentsConfig] | None = Field(default_factory=dict, exclude=True)
    mcp_map: dict[str, TaiMCPConfig] | None = Field(default_factory=dict, exclude=True)

    tools_module_title_map: dict[str, str] | None = Field(default_factory=dict, exclude=True)

    include_module_tools_map: dict[str, frozenset[str]] | None = Field(default_factory=dict, exclude=True)
    exclude_module_tools_map: dict[str, frozenset[str]] | None = Field(default_factory=dict, exclude=True)

    include_module_agents_map: dict[str, frozenset[str]] | None = Field(default_factory=dict, exclude=True)
    exclude_module_agents_map: dict[str, frozenset[str]] | None = Field(default_factory=dict, exclude=True)

    include_title_mcp_tools_map: dict[str, frozenset[str]] | None = Field(default_factory=dict, exclude=True)
    exclude_title_mcp_tools_map: dict[str, frozenset[str]] | None = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def _reject_duplicate_connector_ids(self):
        """Reject two ``connectors`` entries sharing an ``id``: the reload seam
        registers each descriptor by id and the registry's duplicate guard would
        crash the boot — fail loudly here naming the duplicate id instead."""
        seen: set[str] = set()
        for descriptor in self.connectors:
            if descriptor.id in seen:
                raise ValueError(
                    f"manifest: duplicate connector id {descriptor.id!r} — each connectors[*] id must be unique"
                )
            seen.add(descriptor.id)
        return self

    # -- Filter-predicate signatures (impl builds the maps these read) --------

    def should_include_tool(self, name: str, module: str) -> bool:
        """Whether tool ``name`` from ``module`` survives the include/exclude
        filter (and record the decision on the matching config)."""
        raise NotImplementedError(
            "Manifest.should_include_tool is contract-only; the impl Manifest subclass supplies the body"
        )

    def should_include_agent(self, name: str, module: str) -> bool:
        raise NotImplementedError(
            "Manifest.should_include_agent is contract-only; the impl Manifest subclass supplies the body"
        )

    def should_include_mcp_tool(self, name: str, title: str) -> bool:
        raise NotImplementedError(
            "Manifest.should_include_mcp_tool is contract-only; the impl Manifest subclass supplies the body"
        )

    def replace_mcp(self, mcp: list[TaiMCPConfig]) -> None:
        """Swap the MCP rows and rebuild the derived maps (surgical reload)."""
        raise NotImplementedError("Manifest.replace_mcp is contract-only; the impl Manifest subclass supplies the body")

    @property
    def live_manifest(self) -> Manifest:
        """A deep copy whose ``tools``/``agents``/``mcp`` reflect the live
        (post-filter) config maps."""
        raise NotImplementedError(
            "Manifest.live_manifest is contract-only; the impl Manifest subclass supplies the body"
        )

    def find_title(self, module: str) -> str:
        """Map a module path to the owning tool config's title (longest prefix)."""
        raise NotImplementedError("Manifest.find_title is contract-only; the impl Manifest subclass supplies the body")


__all__ = [
    "AgentsConfig",
    "ApiToolsConfig",
    "BaseConfig",
    "ExtensionElement",
    "ExtensionsConfigMixin",
    "MCPConfig",
    "Manifest",
    "TaiMCPConfig",
    "ToolsConfig",
]

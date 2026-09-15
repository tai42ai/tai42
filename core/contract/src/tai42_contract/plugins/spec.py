"""``PluginSpec`` and ``PluginPermissions`` — the complete validated ``tai-plugin.yml``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract._urls import check_web_url
from tai42_contract.plugins.bindings import KIND_MANIFEST_BINDINGS, PluginItemKind
from tai42_contract.plugins.field_validation import (
    DISPLAY_NAME_MAX_LEN,
    ICON_RE,
    LICENSE_RE,
    LISTING_SLUG_RE,
    MIGRATIONS_DIR_RE,
    PACKAGE_RE,
    check_one_line,
    check_tags,
    has_disallowed_control_char,
)
from tai42_contract.plugins.item import PluginItem
from tai42_contract.plugins.routes import route_shape, shapes_overlap
from tai42_contract.plugins.versions import VERSION_RE, check_specifier_clause


class PluginPermissions(BaseModel):
    """Capabilities a plugin declares — informational: surfaced in listings, not enforced by a sandbox.

    Omitting the block declares none (every flag defaults to ``False``); an
    unknown key is rejected loudly rather than silently ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    network: bool = False
    subprocess: bool = False
    filesystem: bool = False


class PluginSpec(BaseModel):
    """The complete, validated content of one ``tai-plugin.yml``.

    Frozen and ``extra="forbid"``: a typo'd key fails validation loudly. The
    listing reference is ``namespace/name`` (:attr:`ref`); ``package`` is the
    normalized pip distribution the listing points at, OPTIONAL because a spec
    whose every item is a data item (``mcp-server``/``connector``) may ship no
    package — a *descriptor-only* plugin that installs nothing but its manifest
    entry. :attr:`delivery` derives the one word every surface shows
    (``"package"`` when ``package`` is set, ``"descriptor"`` when absent); a
    spec with any module item MUST name a package. ``version`` must equal the
    built wheel's version (each plugin repo's spec test and the registry's
    ingest validation both pin that); ``contract`` is the tai42-contract
    compatibility range as a PEP 440 specifier set, required unless every
    provided item is kind ``mcp-server`` (such a package imports no contract),
    and a spec may not mix ``mcp-server`` with any other kind. A connector
    item's ``provider.origin`` is ``system`` iff the listing namespace is
    ``tai42`` — a community listing may not claim the system label.
    ``display_name`` and ``icon`` are optional marketplace display metadata;
    ``premium`` marks a paid listing.

    ``migrations`` is an OPT-IN, package-relative directory holding the plugin's
    ordered SQL schema chain — absent means the plugin owns no tables and is not
    a migrations plugin (most plugins); it requires a ``package``. The contract
    validates only the path's SHAPE; the directory's existence in the installed
    package is enforced at runner-discovery time, not here.

    ``migrations_component`` optionally names the database component the chain
    migrates, for a plugin whose feature store is a SEPARATE, off-unless-declared
    component rather than the distribution itself — absent (the default) runs the
    chain under the distribution name, byte-identical to prior behavior. Setting
    it REQUIRES ``migrations`` (a component with no chain migrates nothing).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: Literal[1]
    namespace: str
    name: str
    display_name: str | None = None
    package: str | None = None
    version: str
    description: str
    icon: str | None = None
    premium: bool = False
    license: str
    homepage: str | None = None
    repository: str | None = None
    contract: str | None = None
    categories: list[str]
    tags: list[str] = Field(default_factory=list)
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    provides: list[PluginItem]
    migrations: str | None = None
    migrations_component: str | None = None

    @property
    def ref(self) -> str:
        """The full listing reference, ``namespace/name``."""
        return f"{self.namespace}/{self.name}"

    @property
    def delivery(self) -> Literal["package", "descriptor"]:
        """How the plugin is delivered: ``"descriptor"`` when it ships no package, else ``"package"``.

        A descriptor is an all-data, install-nothing listing.
        """
        return "descriptor" if self.package is None else "package"

    @field_validator("namespace", "name")
    @classmethod
    def _check_listing_slug(cls, value: str) -> str:
        if not LISTING_SLUG_RE.fullmatch(value):
            raise ValueError(f"namespace/name must match {LISTING_SLUG_RE.pattern}, got {value!r}")
        return value

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = check_one_line(value, field="display_name")
        if len(value) > DISPLAY_NAME_MAX_LEN:
            raise ValueError(f"display_name must be at most {DISPLAY_NAME_MAX_LEN} characters, got {len(value)}")
        return value

    @field_validator("package")
    @classmethod
    def _check_package(cls, value: str | None) -> str | None:
        # Presence is the model validator's concern (required iff any module
        # item); this checks only shape when a value is given.
        if value is None:
            return None
        if not PACKAGE_RE.fullmatch(value):
            raise ValueError(f"package {value!r} must be the normalized pip distribution name")
        return value

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not VERSION_RE.fullmatch(value):
            raise ValueError(f"version {value!r} is not a valid PEP 440 version")
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        return check_one_line(value)

    @field_validator("icon")
    @classmethod
    def _check_icon(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("https://"):
            return check_web_url(value, field="icon", schemes=("https",))
        if not ICON_RE.fullmatch(value):
            raise ValueError(
                f"icon {value!r} must be an https:// URL or a relative POSIX path "
                "(no leading '/', no drive, no backslash)"
            )
        if ".." in value.split("/"):
            raise ValueError(f"icon path {value!r} must not contain a '..' segment")
        return value

    @field_validator("license")
    @classmethod
    def _check_license(cls, value: str) -> str:
        if not LICENSE_RE.fullmatch(value):
            raise ValueError(f"license {value!r} must be an SPDX license id")
        return value

    @field_validator("homepage", "repository")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return check_web_url(value, field="homepage/repository", schemes=("http", "https"))

    @field_validator("contract")
    @classmethod
    def _check_contract_range(cls, value: str | None) -> str | None:
        # Presence is the model validator's concern (required unless every
        # provided item is mcp-server); this checks only shape when a value is
        # given. The stored value must already be clean: the clause-stripping
        # below is only to parse each PEP 440 operator, so a
        # surrounding-or-embedded newline / control char or leading/trailing
        # whitespace would validate yet be stored verbatim. Reject it here (inner
        # whitespace around commas, e.g. ``">=0.1, <0.2"``, stays allowed).
        if value is None:
            return None
        if has_disallowed_control_char(value) or value != value.strip():
            raise ValueError("contract must be a single line with no leading, trailing, or embedded whitespace")
        clauses = [clause.strip() for clause in value.split(",")]
        if "" in clauses:
            raise ValueError("contract must be a non-empty, comma-separated PEP 440 specifier set")
        for clause in clauses:
            check_specifier_clause(clause)
        return value

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, value: list[str]) -> list[str]:
        if not 1 <= len(value) <= 3:
            raise ValueError(f"categories must list 1..3 entries, got {len(value)}")
        if len(set(value)) != len(value):
            raise ValueError("categories must be unique")
        for category in value:
            if not LISTING_SLUG_RE.fullmatch(category):
                raise ValueError(f"category {category!r} must match {LISTING_SLUG_RE.pattern}")
        return value

    @field_validator("tags")
    @classmethod
    def _check_spec_tags(cls, value: list[str]) -> list[str]:
        return check_tags(value)

    @field_validator("provides")
    @classmethod
    def _check_provides(cls, value: list[PluginItem]) -> list[PluginItem]:
        if not value:
            raise ValueError("provides must name at least one item")
        seen: set[tuple[PluginItemKind, str]] = set()
        for item in value:
            key = (item.kind, item.name)
            if key in seen:
                raise ValueError(f"provides has duplicate item {item.kind.value}/{item.name}")
            seen.add(key)
        return value

    @field_validator("migrations")
    @classmethod
    def _check_migrations(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not MIGRATIONS_DIR_RE.fullmatch(value):
            raise ValueError(
                f"migrations {value!r} must be a package-relative POSIX directory path "
                "(no leading '/', no trailing '/', no drive, no backslash)"
            )
        if ".." in value.split("/"):
            raise ValueError(f"migrations path {value!r} must not contain a '..' segment")
        return value

    @model_validator(mode="after")
    def _contract_by_kind(self) -> PluginSpec:
        # One package is one thing: an mcp-server package imports no
        # tai42-contract, so a declared range would be fiction; any other kind
        # needs one. A spec may not mix the two.
        kinds = {item.kind for item in self.provides}
        if PluginItemKind.MCP_SERVER in kinds and len(kinds) > 1:
            others = sorted(kind.value for kind in kinds if kind is not PluginItemKind.MCP_SERVER)
            raise ValueError(
                f"kind {PluginItemKind.MCP_SERVER.value!r} may not share a spec with other kinds; also found {others}"
            )
        if kinds == {PluginItemKind.MCP_SERVER}:
            if self.contract is not None:
                raise ValueError(f"an all-{PluginItemKind.MCP_SERVER.value} spec must not declare 'contract'")
        elif self.contract is None:
            raise ValueError(
                f"'contract' is required unless every provided item is kind {PluginItemKind.MCP_SERVER.value!r}"
            )
        return self

    @model_validator(mode="after")
    def _package_by_payload(self) -> PluginSpec:
        # Delivery axis: a plugin with any code (module-payload) item must name
        # the pip distribution that ships it; an all-data spec may be
        # descriptor-only (no package). ``migrations`` are packaged SQL — a
        # cross-field rule the ``migrations`` field validator cannot see — so
        # they also require a package.
        has_module_item = any(KIND_MANIFEST_BINDINGS[item.kind].payload == "module" for item in self.provides)
        if has_module_item and self.package is None:
            raise ValueError("a plugin with code items must name its package")
        if self.migrations is not None and self.package is None:
            raise ValueError("migrations require a package")
        return self

    @model_validator(mode="after")
    def _migrations_component_requires_migrations(self) -> PluginSpec:
        # A migration component names WHERE a chain runs; naming it with no chain
        # to run is a meaningless declaration, rejected loudly rather than silently
        # ignored.
        if self.migrations_component is not None and self.migrations is None:
            raise ValueError("migrations_component requires migrations")
        return self

    @model_validator(mode="after")
    def _connector_origin_matches_namespace(self) -> PluginSpec:
        # The ``system`` label is reserved for the ``tai42`` namespace: a
        # community listing may not ship a connector marked ``system`` and a
        # ``tai42`` listing may not mark one ``community``. The value is never
        # rewritten — a mismatch is a loud reject.
        for item in self.provides:
            if item.kind is PluginItemKind.CONNECTOR:
                if item.provider is None:
                    raise AssertionError
                if (item.provider.origin == "system") != (self.namespace == "tai42"):
                    raise ValueError(
                        f"connector {item.name!r} origin {item.provider.origin!r} must be 'system' "
                        f"iff namespace is 'tai42' (namespace is {self.namespace!r})"
                    )
        return self

    @model_validator(mode="after")
    def _no_route_collisions(self) -> PluginSpec:
        # One-owner-per-route within the spec: two declared rows collide when
        # their resolved shapes overlap AND their method sets intersect. Same
        # shape with disjoint methods is legal. Plugin declarations never carry
        # converters, so a literal/template overlap is the whole rule here.
        declared: list[tuple[str, str, tuple[str | None, ...], frozenset[str]]] = []
        for item in self.provides:
            if item.routes is None:
                continue
            base = item.routes.base
            for route in item.routes.paths:
                shape = route_shape(base, route.path)
                methods = frozenset(route.methods)
                for other_name, other_path, other_shape, other_methods in declared:
                    shared = methods & other_methods
                    if shared and shapes_overlap(shape, other_shape):
                        raise ValueError(
                            f"route collision: item {item.name!r} path {route.path!r} overlaps "
                            f"item {other_name!r} path {other_path!r} on methods {sorted(shared)}"
                        )
                declared.append((item.name, route.path, shape, methods))
        return self

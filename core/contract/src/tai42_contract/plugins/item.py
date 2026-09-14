"""``PluginItem`` — one installable item in a plugin's ``provides`` index."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.manifest import MCPConfig
from tai42_contract.plugins.bindings import DATA_BLOCK_BY_KIND, PluginItemKind
from tai42_contract.plugins.field_validation import ITEM_NAME_RE, MODULE_RE, TAG_RE, check_one_line, check_tags
from tai42_contract.plugins.routes import RoutesDecl


class PluginItem(BaseModel):
    """One installable item in a plugin's ``provides`` index.

    ``module`` is the import path whose import side-effect registers the item
    (or, for env-selected kinds, the module the selecting seam imports); the
    installer patches it into the manifest per ``KIND_MANIFEST_BINDINGS``.

    The item shape is table-driven on ``KIND_MANIFEST_BINDINGS`` (a model
    validator enforces it, loud both ways): a data-payload kind carries exactly
    the declarative block :data:`DATA_BLOCK_BY_KIND` names for it — ``mcp``
    transport config for ``mcp-server``, a ``provider`` descriptor for
    ``connector`` — and no ``module``; every module-payload kind carries
    ``module`` and no data block. A ``connector`` item's ``name`` MUST equal its
    ``provider.id`` — that id is the manifest/uninstall key.

    ``routes`` declares the item's HTTP mount: REQUIRED for ``kind: router``,
    OPTIONAL for ``kind: channel``, and FORBIDDEN (must be absent) for every
    other kind.

    ``group`` is an OPTIONAL logical family label the author may put on any item
    of any kind: items sharing a value belong to one family, and a consumer may
    summarize the ``provides`` index by group. It is purely a structural
    self-description of the plugin — cross-kind groups and single-member groups
    are both legal, and absent means the item stands alone. It carries a tag's
    charset discipline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PluginItemKind
    name: str
    module: str | None = None
    mcp: MCPConfig | None = None
    provider: ProviderDescriptor | None = None
    description: str
    tags: list[str] = Field(default_factory=list)
    group: str | None = None
    routes: RoutesDecl | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not ITEM_NAME_RE.fullmatch(value):
            raise ValueError(f"item name {value!r} must match {ITEM_NAME_RE.pattern}")
        return value

    @field_validator("module")
    @classmethod
    def _check_module(cls, value: str | None) -> str | None:
        # Presence is the model validator's concern (module is required for the
        # module-payload kinds, not the data-payload mcp-server/connector); this
        # checks only shape when a value is given.
        if value is None:
            return None
        if not MODULE_RE.fullmatch(value):
            raise ValueError(f"module {value!r} must be a dotted Python import path")
        return value

    @field_validator("description")
    @classmethod
    def _check_description(cls, value: str) -> str:
        return check_one_line(value)

    @field_validator("tags")
    @classmethod
    def _check_item_tags(cls, value: list[str]) -> list[str]:
        return check_tags(value)

    @field_validator("group")
    @classmethod
    def _check_group(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not TAG_RE.fullmatch(value):
            raise ValueError(f"group {value!r} must match {TAG_RE.pattern}")
        return value

    def _check_module_item_shape(self) -> None:
        # A module-payload kind carries ``module`` and no data block.
        if self.module is None:
            raise ValueError(f"kind {self.kind.value!r} requires 'module'")
        for block in DATA_BLOCK_BY_KIND.values():
            if getattr(self, block) is not None:
                raise ValueError(f"kind {self.kind.value!r} must not set {block!r}")

    def _check_data_item_shape(self, expected_block: str) -> None:
        # A data-payload kind carries exactly the block :data:`DATA_BLOCK_BY_KIND`
        # names for it and no ``module`` (nor any other data block).
        if getattr(self, expected_block) is None:
            raise ValueError(f"kind {self.kind.value!r} requires {expected_block!r}")
        if self.module is not None:
            raise ValueError(f"kind {self.kind.value!r} must not set 'module'")
        for block in DATA_BLOCK_BY_KIND.values():
            if block != expected_block and getattr(self, block) is not None:
                raise ValueError(f"kind {self.kind.value!r} must not set {block!r}")
        if self.kind is PluginItemKind.MCP_SERVER:
            # A static mcp-server spec must name a reachable transport:
            # MCPConfig permits zero-transport only for runtime entries built
            # empty then mutated, so require it loudly at the plugin boundary
            # rather than letting an empty shell surface late at spawn. Its
            # own validator already guarantees at MOST one of
            # url/uds/command; this adds the at-least-one requirement.
            assert self.mcp is not None
            if self.mcp.url is None and self.mcp.uds is None and self.mcp.command is None:
                raise ValueError(f"kind {self.kind.value!r} 'mcp' must declare a transport (url/uds/command)")
        elif self.kind is PluginItemKind.CONNECTOR:
            # ``provider.id`` is the manifest ``connectors`` key and the
            # uninstall key, so the item name must equal it.
            assert self.provider is not None
            if self.name != self.provider.id:
                raise ValueError(f"connector item name {self.name!r} must equal provider.id {self.provider.id!r}")

    def _check_routes_shape(self) -> None:
        # ``routes`` is required for a router, optional for a channel, forbidden elsewhere.
        if self.kind is PluginItemKind.ROUTER:
            if self.routes is None:
                raise ValueError(f"kind {self.kind.value!r} requires 'routes'")
        elif self.kind is not PluginItemKind.CHANNEL and self.routes is not None:
            raise ValueError(f"kind {self.kind.value!r} must not set 'routes'")

    @model_validator(mode="after")
    def _shape_by_kind(self) -> PluginItem:
        # Table-driven on the payload axis: a data kind carries exactly the
        # block :data:`DATA_BLOCK_BY_KIND` names for it and no ``module``; a
        # module kind carries ``module`` and no data block. Then the routes rule.
        expected_block = DATA_BLOCK_BY_KIND.get(self.kind)
        if expected_block is None:
            self._check_module_item_shape()
        else:
            self._check_data_item_shape(expected_block)
        self._check_routes_shape()
        return self

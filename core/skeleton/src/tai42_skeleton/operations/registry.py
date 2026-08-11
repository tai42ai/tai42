"""The operation metadata record and the process-wide operation registry.

An :class:`OperationMetadata` carries everything the four downstream consumers
need from one declaration: the route adapter (route template + method + request
model + declared errors), the OpenAPI emitter (``destructive`` → ``x-destructive``),
the tool projection (name, destructive → ``destructiveHint``, reload gate), and
the tool-edge authorization (route template + method for the concrete-path
synthesis the scope verifier and fences key on).

The decorator fills the operation-level fields; the adapter registration attaches
the route template and HTTP method to the SAME record, because authz's path
synthesis and the fences' ``{"method", "path"}`` context need both.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from tai42_skeleton.operations.errors import OperationError


@dataclass
class OperationMetadata:
    """One operation's declared metadata — the single source the route, the CLI,
    the OpenAPI spec, the MCP tool, and the authorization check all derive from."""

    name: str
    func: Callable[..., Awaitable[object]]
    summary: str
    tags: tuple[str, ...] = ()
    destructive: bool = False
    reload_gated: bool = False
    meta_executor: bool = False
    caller_context: bool = False
    authority_changing: bool = False
    error_classes: tuple[type[OperationError], ...] = ()
    request_model: type[BaseModel] | None = None
    response_model: type[BaseModel] | None = None
    # A model whose fields are published as ``in: query`` parameters for ANY method,
    # additive to ``request_model`` — the channel a WRITE-method door declares the query
    # it reads at the edge (a read door's ``request_model`` already emits as query).
    query_model: type[BaseModel] | None = None

    # Attached by the route-adapter registration (not the decorator): the route
    # template (``/api/tools/{name}``) and the HTTP method the operation serves.
    # Authz synthesizes the concrete path from these + the call's path args.
    route_template: str | None = None
    http_method: str | None = None

    # Filled from the adapter's ``path_params`` so authz knows which call
    # arguments name path segments in ``route_template``.
    path_params: tuple[str, ...] = field(default_factory=tuple)


class OperationRegistry:
    """In-memory map of every registered operation, keyed by operation name.

    Registering the SAME name twice raises loudly — mirroring the tool binding's
    duplicate-name guard — so two operations cannot silently claim one name.

    The registry also states whether it is SETTLED, i.e. whether its silence about a
    name is an answer. A boot/reload tears it down and replays it while the serving
    loop keeps dispatching, so a reader that must distinguish "not an operation" from
    "not replayed yet" consults :attr:`settled` rather than guessing from the contents.
    """

    def __init__(self) -> None:
        # ``_operations`` is the COMMITTED generation the request path (authz/dispatch)
        # resolves against; ``_pending`` is the generation an epoch build stages into,
        # promoted to committed in ONE reference assignment (atomic under the GIL) only
        # on a successful build. A failed build drops ``_pending`` and never touches the
        # committed surface, so the live epoch keeps dispatching against a complete
        # generation — the request path never observes a torn rebuild.
        self._operations: dict[str, OperationMetadata] = {}
        self._pending: dict[str, OperationMetadata] | None = None
        self._rebuild_depth = 0

    def _write_target(self) -> dict[str, OperationMetadata]:
        return self._pending if self._pending is not None else self._operations

    def register(self, metadata: OperationMetadata) -> None:
        target = self._write_target()
        existing = target.get(metadata.name)
        if existing is not None and existing is not metadata:
            raise ValueError(
                f"Operation {metadata.name!r} is already registered; "
                "an operation name must be unique across the registry."
            )
        target[metadata.name] = metadata

    def get(self, name: str) -> OperationMetadata:
        try:
            return self._operations[name]
        except KeyError:
            raise KeyError(f"Operation {name!r} is not registered.") from None

    def has(self, name: str) -> bool:
        return name in self._operations

    def all(self) -> list[OperationMetadata]:
        """Every COMMITTED operation, ordered by name — the request-path surface."""
        return [self._operations[name] for name in sorted(self._operations)]

    def names(self) -> frozenset[str]:
        return frozenset(self._operations)

    def all_staged(self) -> list[OperationMetadata]:
        """Every operation in the STAGED generation if a build is staging, else the
        committed one — the build's own view (the projection reads this so it projects
        the generation being assembled, not the live one)."""
        target = self._write_target()
        return [target[name] for name in sorted(target)]

    def names_staged(self) -> frozenset[str]:
        """The names in the STAGED generation if a build is staging, else committed —
        the build's own view (the projection's include/exclude validation)."""
        return frozenset(self._write_target())

    def clear(self) -> None:
        """Drop every registration from the write target (the staged generation while a
        build is staging, else the committed surface). Kept for tests; the epoch build
        opens an empty staged generation via :meth:`begin_staging` rather than clearing."""
        self._write_target().clear()

    def begin_staging(self) -> None:
        """Open a fresh staged generation the epoch build populates, leaving the
        committed surface serving the live generation untouched."""
        self._pending = {}

    def commit_staging(self) -> None:
        """Promote the staged generation to committed in one reference assignment
        (atomic under the GIL). A no-op if no build staged."""
        if self._pending is not None:
            self._operations = self._pending
            self._pending = None

    def abort_staging(self) -> None:
        """Drop the staged generation on a failed build — the committed surface never
        saw any of its registrations."""
        self._pending = None

    @contextmanager
    def rebuilding(self) -> Iterator[None]:
        """Mark the surface UNSETTLED for the duration of a teardown + replay.

        The boot/reload body wraps the whole rebuild in this — the ``clear()``, the
        snapshot replay, and the router re-attachment that puts each record's route
        template back — because a record is only usable once all three have run. It
        nests (a reload reached from inside another rebuild keeps the mark held until
        the outermost one finishes) and releases in a ``finally``, so a rebuild that
        raises cannot leave the surface permanently marked torn.
        """
        self._rebuild_depth += 1
        try:
            yield
        finally:
            self._rebuild_depth -= 1

    @property
    def settled(self) -> bool:
        """Whether this registry's contents are an ANSWER: no rebuild is in flight AND
        it is non-empty.

        Emptiness alone is decisive: a started app always holds this package's leaf
        operations, so an empty registry is only ever the cleared half of a rebuild (or
        an app that never started). Both halves must hold — a rebuild that has already
        replayed some records is populated but not yet an answer about the rest.
        """
        return self._rebuild_depth == 0 and bool(self._operations)


# The one process-wide operation registry. The ``@operation`` decorator records
# into it; the adapter, the emitter, the projection, and authz read it.
operation_registry = OperationRegistry()

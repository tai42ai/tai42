"""Operations for the subject-keyed state store — ``/api/states*``, the sibling
``/api/state-modules*`` and ``/api/state-retention/prune``.

Thin, request-free operations over the ``tai42_app.states`` facet (the HTTP routes in
:mod:`tai42_skeleton.routers.states` are their adapters). Every operation runs its facet
call inside :func:`_states_door`, which maps the store's typed errors
(:mod:`tai42_contract.states.errors`) to the operation error the adapter answers with —
501 not-configured, 404 not-found, 409 exists/in-use/conflict, 422 schema/subject/regime/
path/value, 412 narrowing-needs-confirmation — so one mapping governs every door.

A write door supplies an empty :class:`~tai42_contract.states.WriteOrigin`: the platform
write chokepoint completes it with door ``api`` + the request principal (no ambient state
context is deposited on an HTTP call), so the audit ledger records who wrote through the
API without the door forging provenance.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import ValidationError
from tai42_contract.states import (
    MountBody,
    StateDeclaration,
    StateModuleDocument,
    StateSubject,
    WriteOrigin,
)
from tai42_contract.states.errors import (
    DeclarationInUseError,
    InvalidPathError,
    MigrationConversionError,
    ModuleExistsError,
    ModuleInUseError,
    ModuleValidationError,
    MountConflictError,
    NarrowingRequiresConfirmationError,
    NonAdditiveRedeclareError,
    RegimeViolationError,
    SchemaValidationError,
    StateExistsError,
    StateNotFoundError,
    StatesError,
    StatesNotConfiguredError,
    SubjectFoldError,
    SubjectRefusedError,
    ValueValidationError,
)

from tai42_skeleton.app import instance
from tai42_skeleton.operations import (
    ConflictError,
    NotFoundError,
    NotSupportedError,
    PreconditionFailedError,
    ValidationRejected,
    operation,
)

# The machine-readable code the states OFF refusal carries; the message is the store's own.
_NOT_CONFIGURED_CODE = "states-not-configured"

# The store's typed errors → the operation error (hence HTTP status) the door answers.
# StatesNotConfiguredError is handled first (it carries the stable OFF code); every other
# class maps by exact type, so a new error class added upstream fails LOUDLY as a 500
# rather than being silently mis-mapped.
_ERROR_MAP: dict[type[StatesError], type] = {
    StateNotFoundError: NotFoundError,
    StateExistsError: ConflictError,
    NonAdditiveRedeclareError: ConflictError,
    DeclarationInUseError: ConflictError,
    SubjectFoldError: ConflictError,
    ModuleExistsError: ConflictError,
    ModuleInUseError: ConflictError,
    MountConflictError: ConflictError,
    NarrowingRequiresConfirmationError: PreconditionFailedError,
    SubjectRefusedError: ValidationRejected,
    SchemaValidationError: ValidationRejected,
    MigrationConversionError: ValidationRejected,
    InvalidPathError: ValidationRejected,
    ValueValidationError: ValidationRejected,
    RegimeViolationError: ValidationRejected,
    ModuleValidationError: ValidationRejected,
}


@contextmanager
def _states_door() -> Iterator[None]:
    """Translate the store's typed errors into the operation error the adapter maps to a
    status. The one seam every states operation runs its facet call through."""
    try:
        yield
    except StatesNotConfiguredError as exc:
        raise NotSupportedError(str(exc), extra={"code": _NOT_CONFIGURED_CODE}) from exc
    except StatesError as exc:
        mapped = _ERROR_MAP.get(type(exc))
        if mapped is None:
            raise
        raise mapped(str(exc)) from exc


def _states():
    return instance.app.states


def _subject(target_kind: str, target_name: str, kind: str, key: str) -> StateSubject:
    """Build a :class:`StateSubject` from the record route's path segments; a malformed
    segment (a bad target kind, an over-long key) is a 422 rejected input."""
    try:
        return StateSubject.model_validate(
            {"target_kind": target_kind, "target_name": target_name, "kind": kind, "key": key}
        )
    except ValidationError as exc:
        raise ValidationRejected(f"invalid subject: {exc.errors(include_url=False)}") from exc


# --------------------------------------------------------------------------- #
# Declarations                                                                 #
# --------------------------------------------------------------------------- #
@operation(summary="List declared states", tags=["states"], errors=[NotSupportedError])
async def list_states() -> list[dict[str, Any]]:
    """Every declared state, base + composed effective schema included, each with its
    ``updated_at`` timestamp (the Updated column)."""
    with _states_door():
        # ``mode="json"`` so the served ``updated_at`` datetime encodes as an ISO string;
        # a python-mode dump leaves a raw datetime the JSON response cannot serialize.
        return [decl.model_dump(mode="json") for decl in await _states().list_declarations()]


@operation(summary="Get a state", tags=["states"], errors=[NotSupportedError, NotFoundError])
async def get_state(name: str) -> dict[str, Any]:
    """One state's served declaration: base ``schema``, ``effective_schema``,
    ``subject_kinds``, ``default_subject_kind``, its ``mounts``, computed ``regimes`` and
    ``updated_at`` (the ISO timestamp of its last write)."""
    with _states_door():
        return await _states().served_declaration(name)


@operation(
    summary="Create or re-declare a state",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, ValidationRejected, ConflictError, NotFoundError],
)
async def put_state(name: str, declaration: dict[str, Any]) -> dict[str, Any]:
    """Upsert a state declaration; the ``name`` is taken from the path. With records present
    only additive schema changes are accepted — a narrowing goes through ``migrate``."""
    body = {**declaration, "name": name}
    try:
        decl = StateDeclaration.model_validate(body)
    except ValidationError as exc:
        raise ValidationRejected(f"invalid declaration: {exc.errors(include_url=False)}") from exc
    with _states_door():
        try:
            saved = await _states().put_declaration(decl)
        except ValueError as exc:
            # ``effective_schema`` supplied by a client (computed by the platform) is a
            # rejected input, not a store fault.
            raise ValidationRejected(str(exc)) from exc
    return saved.model_dump(mode="json")


@operation(
    summary="Delete a state",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ConflictError],
)
async def delete_state(name: str) -> dict[str, Any]:
    """Delete a state with its records, mounts and aliases; refused while a consumer binds it."""
    with _states_door():
        await _states().delete_declaration(name)
    return {"deleted": True, "name": name}


@operation(summary="A state's record statistics", tags=["states"], errors=[NotSupportedError, NotFoundError])
async def state_stats(name: str) -> dict[str, Any]:
    """Record counts for a state — the total and the per-subject-kind breakdown."""
    with _states_door():
        return await _states().stats(name)


@operation(
    summary="Migrate a state's schema",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected, PreconditionFailedError],
)
async def migrate_state(
    name: str,
    new_schema: dict[str, Any],
    transform_expr: str | None = None,
    confirm_drop: bool = False,
    resolutions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Migrate every record to ``new_schema`` in one transaction. A narrowing needs exactly
    one of a ``transform_expr`` or ``confirm_drop`` (with optional ``resolutions``); without
    it the door answers 412."""
    with _states_door():
        await _states().migrate(
            name,
            new_schema,
            origin=WriteOrigin(),
            transform_expr=transform_expr,
            confirm_drop=confirm_drop,
            resolutions=resolutions,
        )
    return {"migrated": True, "name": name}


@operation(
    summary="Preview a state migration",
    tags=["states"],
    errors=[NotSupportedError, NotFoundError, ValidationRejected],
)
async def preview_migrate_state(name: str, new_schema: dict[str, Any]) -> dict[str, Any]:
    """Dry-run a migration to ``new_schema``: whether it narrows, and example records the
    change would drop or need resolving — no record is written."""
    with _states_door():
        return await _states().preview_migrate(name, new_schema)


# --------------------------------------------------------------------------- #
# Mounts                                                                       #
# --------------------------------------------------------------------------- #
@operation(summary="List a state's mounts", tags=["states"], errors=[NotSupportedError, NotFoundError])
async def list_state_mounts(name: str) -> list[dict[str, Any]]:
    """Every module mounted on the state — its path, resolved parameters and declarations."""
    with _states_door():
        return await _states().list_mounts(name)


@operation(
    summary="Mount a module on a state",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected, ConflictError],
)
async def mount_state_module(name: str, module: str, body: dict[str, Any]) -> dict[str, Any]:
    """Mount ``module`` on the state at the body's ``path`` with its parameters and static
    declarations; recomposes and validates the effective schema in one transaction."""
    try:
        mount_body = MountBody.model_validate(body)
    except ValidationError as exc:
        raise ValidationRejected(f"invalid mount body: {exc.errors(include_url=False)}") from exc
    with _states_door():
        await _states().mount(name, module, mount_body)
    return {"mounted": True, "state": name, "module": module}


@operation(
    summary="Update a mount's declarations",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected, ConflictError],
)
async def update_state_mount(name: str, module: str, declarations: dict[str, Any]) -> dict[str, Any]:
    """Replace a mount's static declaration values, re-running the mount validators and
    recomposing the effective schema."""
    with _states_door():
        await _states().update_mount_declarations(name, module, declarations)
    return {"updated": True, "state": name, "module": module}


@operation(
    summary="Unmount a module from a state",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError],
)
async def unmount_state_module(name: str, module: str) -> dict[str, Any]:
    """Remove a module's mount and recompose the effective schema — nothing else."""
    with _states_door():
        await _states().unmount(name, module)
    return {"unmounted": True, "state": name, "module": module}


# --------------------------------------------------------------------------- #
# Subjects + records                                                           #
# --------------------------------------------------------------------------- #
@operation(summary="List a state's subjects", tags=["states"], errors=[NotSupportedError, NotFoundError])
async def list_state_subjects(
    name: str, kind: str | None = None, limit: int | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """A keyset page of the subjects holding a record for the state, optionally one ``kind``."""
    with _states_door():
        return await _states().list_subjects(name, kind=kind, limit=limit, cursor=cursor)


@operation(summary="Search a state's records", tags=["states"], errors=[NotSupportedError, NotFoundError])
async def search_state_records(
    name: str, filters: dict[str, Any], limit: int | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """A keyset page of records whose document contains ``filters`` (a JSON containment match)."""
    with _states_door():
        return await _states().search(name, filters, limit=limit, cursor=cursor)


@operation(
    summary="Read a subject's record",
    tags=["states"],
    errors=[NotSupportedError, NotFoundError, ValidationRejected],
)
async def read_state_record(
    name: str, target_kind: str, target_name: str, kind: str, key: str
) -> dict[str, Any] | None:
    """One subject's record, or ``null`` when none exists. An unknown ``person`` subject is a
    refusal, never an empty document."""
    subject = _subject(target_kind, target_name, kind, key)
    with _states_door():
        view = await _states().read(name, subject)
    return None if view is None else view.model_dump()


@operation(
    summary="Replace a subject's record",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected],
)
async def replace_state_record(
    name: str, target_kind: str, target_name: str, kind: str, key: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Replace a subject's whole document with ``data`` (validated against the effective schema)."""
    subject = _subject(target_kind, target_name, kind, key)
    with _states_door():
        view = await _states().replace(name, subject, data, origin=WriteOrigin())
    return view.model_dump()


@operation(
    summary="Merge into a subject's record",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected],
)
async def merge_state_record(
    name: str, target_kind: str, target_name: str, kind: str, key: str, patch: dict[str, Any]
) -> dict[str, Any]:
    """Shallow top-level merge of ``patch`` into a subject's document."""
    subject = _subject(target_kind, target_name, kind, key)
    with _states_door():
        view = await _states().merge(name, subject, patch, origin=WriteOrigin())
    return view.model_dump()


@operation(
    summary="Apply ops to a subject's record",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected],
)
async def apply_state_record(
    name: str,
    target_kind: str,
    target_name: str,
    kind: str,
    key: str,
    ops: list[dict[str, Any]],
    op_id: str | None = None,
) -> dict[str, Any]:
    """Apply an ordered op batch to a subject's document — refused if a whole-path write
    violates a mounted path's regime; idempotent on a replayed ``op_id``."""
    subject = _subject(target_kind, target_name, kind, key)
    with _states_door():
        result = await _states().apply(name, subject, ops, op_id=op_id, origin=WriteOrigin())
    return result.model_dump()


@operation(
    summary="Erase a subject's record",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected],
)
async def erase_state_record(name: str, target_kind: str, target_name: str, kind: str, key: str) -> dict[str, Any]:
    """Erase a subject's record; a fresh read then returns ``null``."""
    subject = _subject(target_kind, target_name, kind, key)
    with _states_door():
        await _states().erase(name, subject, origin=WriteOrigin())
    return {"erased": True}


@operation(
    summary="Fold one subject into another",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ValidationRejected, ConflictError],
)
async def fold_state_record(
    name: str,
    target_kind: str,
    target_name: str,
    kind: str,
    key: str,
    into: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Fold this subject's record into the ``into`` subject under ``mode`` (an alias the reads
    then resolve through). Refused on a self-fold, a cycle, or a merge that no longer validates."""
    subject = _subject(target_kind, target_name, kind, key)
    try:
        into_subject = StateSubject.model_validate(into)
    except ValidationError as exc:
        raise ValidationRejected(f"invalid fold target: {exc.errors(include_url=False)}") from exc
    with _states_door():
        return await _states().fold(name, subject, into_subject, mode, origin=WriteOrigin())


@operation(
    summary="A subject's write audit trail",
    tags=["states"],
    errors=[NotSupportedError, NotFoundError, ValidationRejected],
)
async def list_state_writes(
    name: str,
    target_kind: str,
    target_name: str,
    kind: str,
    key: str,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One keyset page of a subject's write ledger: ``items`` (each entry's seq, timestamp,
    completed origin (door/actor/consumer/meta/run/turn) and the paths it touched) and the
    ``next_cursor`` the next page reads from (null on the last page)."""
    subject = _subject(target_kind, target_name, kind, key)
    with _states_door():
        page = await _states().writes(name, subject, limit=limit, cursor=cursor)
    return page.model_dump(mode="json")


@operation(summary="A state's consumers", tags=["states"], errors=[NotSupportedError, NotFoundError])
async def state_consumers(name: str) -> list[dict[str, Any]]:
    """Everything that binds the state — flows, hooks, schedules, agents — as the Consumers
    tab reads it; a consumer family the deployment cannot list is a labelled, muted row."""
    with _states_door():
        return [row.model_dump() for row in await _states().consumers(name)]


# --------------------------------------------------------------------------- #
# Modules (the sibling collection) + retention                                #
# --------------------------------------------------------------------------- #
@operation(summary="List state modules", tags=["states"], errors=[NotSupportedError])
async def list_state_modules() -> list[dict[str, Any]]:
    """Every platform state-module document (the reusable schema fragments), each with the
    catalog columns ``mounted_on`` (the number of states it is mounted on) and
    ``shipped_default`` (whether it is an unedited shipped default)."""
    with _states_door():
        return await _states().list_modules_catalog()


@operation(summary="Get a state module", tags=["states"], errors=[NotSupportedError, NotFoundError])
async def get_state_module(name: str) -> dict[str, Any]:
    """One state-module document by name."""
    with _states_door():
        doc = await _states().get_module(name)
    if doc is None:
        raise NotFoundError(f"no state module named {name!r}")
    return doc.model_dump()


@operation(
    summary="Create or replace a state module",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, ValidationRejected, ConflictError],
)
async def put_state_module(name: str, document: dict[str, Any], replace: bool = False) -> dict[str, Any]:
    """Upload a state-module document; the ``name`` is taken from the path. An existing name
    without ``replace`` is refused so an overwrite is deliberate."""
    body = {**document, "name": name}
    try:
        doc = StateModuleDocument.model_validate(body)
    except ValidationError as exc:
        raise ValidationRejected(f"invalid module document: {exc.errors(include_url=False)}") from exc
    with _states_door():
        saved = await _states().put_module(doc, replace=replace)
    return saved.model_dump()


@operation(
    summary="Delete a state module",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError, NotFoundError, ConflictError],
)
async def delete_state_module(name: str) -> dict[str, Any]:
    """Delete a state-module document; refused while it is still mounted."""
    with _states_door():
        existing = await _states().get_module(name)
        if existing is None:
            raise NotFoundError(f"no state module named {name!r}")
        await _states().delete_module(name)
    return {"deleted": True, "name": name}


@operation(
    summary="Prune expired state records",
    tags=["states"],
    destructive=True,
    errors=[NotSupportedError],
)
async def prune_state_retention() -> dict[str, Any]:
    """Delete every record past its state's ``retention_days`` horizon; returns the per-state
    deleted counts."""
    with _states_door():
        return {"pruned": await _states().prune_expired()}

"""The preset create path.

The ordered pre-write gates, the locked name claim (store write THEN register),
the create door, and the declared-seed appliers.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from tai42_contract.agent.base import PresetSpec
from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import PresetBody, PresetSeed
from tai42_contract.presets.errors import (
    PresetExistsError,
    PresetNameConflictError,
    PresetNotFoundError,
)
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotSupportedError,
    operation,
)
from tai42_skeleton.operations.presets import fanout
from tai42_skeleton.operations.presets.authoring import (
    _attach_body_binding,
    _combo_registry_error,
    _dry_run_bind_error,
    _enforce_registration_tier,
    _input_schema_authoring_error,
    _output_schema_error,
    _write_validator_error,
)
from tai42_skeleton.operations.presets.models import PresetCreate
from tai42_skeleton.operations.presets.views import _create_response
from tai42_skeleton.operations.response_models_group_a import PresetCreateResult
from tai42_skeleton.presets.manager import is_valid_preset_name

logger = logging.getLogger(__name__)

# This submodule's own package generation, captured at import time (a reload builds a
# fresh package + submodules together, so each generation's submodule reads its OWN
# package). ``_agent_tool_names``, ``_agent_authoring_error`` and ``advisory_name_lock``
# are read through it at call time, so a test's ``monkeypatch.setattr`` on the package
# attribute is honored.
_pkg = sys.modules["tai42_skeleton.operations.presets"]

# The machine-readable code every preset OFF refusal carries when the
# versioned-document store is unconfigured; the message is rendered from the live
# binding at raise time. A 501 (a capability the deployment lacks), never a
# transient 503 — the store is absent, not momentarily down.
_NOT_CONFIGURED_CODE = "versioning-not-configured"
_NOT_CONFIGURED_NOUN = "versioned-document store"

# The advisory-lock namespace every preset create claims its NAME in ("pset" as ASCII
# bytes, a positive int32). A namespace of its own keeps these locks from excluding
# another feature that happens to lock the same name.
_PRESET_LOCK_NAMESPACE = 0x70736574


async def _check_create_name(name: str, mgr: Any) -> None:
    """The ordered name pre-checks a create runs before any store write.

    Name safety (tool-name-safe), then the quarantine 409, the live-tool collision
    409, the agent tool-name collision 400, and the duplicate-preset 409 — the
    quarantine verdict winning for a name that would also collide. Raises the
    mapped 400/409.
    """
    # A preset name is a live tool name + a ``{name}`` route segment, so it must be
    # tool-name-safe (a slash-bearing name would never match the routes; an
    # over-long one collides after client-tool truncation).
    if not is_valid_preset_name(name):
        raise BadRequestError(f"invalid preset name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$")
    if mgr.is_quarantined(name):
        raise ConflictError(f"a quarantined preset {name!r} exists — delete the quarantined record first")
    if await mgr.name_conflicts(name):
        raise ConflictError(f"preset name {name!r} collides with an existing tool")
    # The same collision guard, extended to the agent-name space: an agent's
    # registration name is already a live tool (caught above), but its ``tool_name``
    # may not be — keep the authored name off that set too so one agent-name space
    # stays unambiguous. Read through the package object so a monkeypatch bites.
    if name in _pkg._agent_tool_names():
        raise BadRequestError(f"preset name {name!r} collides with an agent tool name")
    if mgr.is_registered(name):
        raise ConflictError(f"preset {name!r} already exists")


async def _check_create_base(base_tool: str, mgr: Any) -> None:
    """A preset's base must be a registered NON-preset tool.

    A preset cannot be another preset's base — chaining would make rehydration
    order-dependent. Raises the mapped 400.
    """
    if base_tool in mgr.registered_names():
        raise BadRequestError(f"base tool {base_tool!r} is itself a preset")
    if base_tool not in await instance.app.tools.get_tools():
        raise BadRequestError(f"base tool {base_tool!r} is not a registered tool")


async def _claim_preset_name(name: str, body: PresetBody, tags: list[str] | None) -> tuple[Any, dict[str, int] | None]:
    """Claim ``name`` for a new preset under the fleet-wide per-name advisory lock.

    The store-side existence check, the overlay clean slate, the store write and
    the local register (rolled fully back on a register failure) are ONE step no
    other process can interleave with. Returns the store record plus the fan-out
    census pinned before the first write, which the caller publishes with the lock
    already released.

    The create path acquires that lock here and nowhere else, and nothing under it creates
    a preset, so a create takes it exactly once: the lock runs on a connection of its own,
    so a nested acquire would wait on a lock its own caller holds.
    """
    # The advisory lock is read through the package object so a monkeypatch bites.
    async with _pkg.advisory_name_lock(_PRESET_LOCK_NAMESPACE, name):
        # The name's existence is re-read from the STORE here: the pre-checks above read
        # THIS worker's registry, which a sibling process's create never reaches. An
        # existing preset conflicts before the clean-slate cascade below, so a create of
        # a name already taken never touches that preset's tool_meta overlay.
        try:
            await instance.app.presets.store.get_preset(name)
        except PresetNotFoundError:
            pass
        else:
            raise ConflictError(f"preset {name!r} already exists")

        # Pin the fleet BEFORE the first write below: the cascade, the store write and
        # the register are all this door's local apply, and the fan-out censuses only
        # when it publishes, on the far side of every one of them.
        census = await fanout._census_at_start(fanout._RELOAD_OP)

        # Clean slate: a dangling overlay row for this name (left by a DIFFERENT tool
        # that once held it, kept across a plugin uninstall) must never be inherited by
        # the fresh preset, so drop it before the claim. A no-op when no row exists; and
        # if the create below rolls back, the deleted ghost belonged to a vanished tool
        # and needs no restoring.
        await instance.app.tool_meta.store.delete_meta(name)

        # The pre-checks already ran, so the store write is safe. Persist THEN register;
        # if register fails, roll the store row fully back through the generic HARD
        # delete so no stored-but-unregistered preset survives.
        spec = PresetSpec(
            name=name, description=body.description, base_tool=body.base_tool, fixed_kwargs=body.fixed_kwargs
        )
        try:
            record = await instance.app.presets.store.create_preset(
                spec,
                extensions=body.extensions,
                output_schema=body.output_schema,
                input_schema=body.input_schema,
                state_binding=body.state_binding,
                tags=tags,
            )
        except PresetNameConflictError as exc:
            raise ConflictError(f"preset name {name!r} collides with an existing tool") from exc
        except PresetExistsError as exc:
            raise ConflictError(f"preset {name!r} already exists") from exc
        try:
            await instance.app.preset_manager.register(
                name,
                body.base_tool,
                body.fixed_kwargs,
                body.extensions,
                body.description,
                body.output_schema,
                body.input_schema,
                state_binding=body.state_binding,
                version=record.active_version,
            )
        except Exception as register_exc:
            try:
                await instance.app.versioning.store.delete("preset", name)
            except Exception as delete_exc:
                logger.exception("failed to roll back store row for preset %r after a register failure", name)
                raise delete_exc from register_exc
            # A typed clobber error (the name was taken by a foreign tool in the window
            # between the pre-checks and the register) maps to 409; any other register
            # failure re-raises loudly.
            if isinstance(register_exc, PresetExistsError):
                raise ConflictError(f"preset {name!r} already exists") from register_exc
            if isinstance(register_exc, PresetNameConflictError):
                raise ConflictError(f"preset name {name!r} collides with an existing tool") from register_exc
            raise
    return record, census


async def _create_preset_core(
    name: str,
    base_tool: str,
    description: str,
    fixed_kwargs: dict[str, Any],
    extensions: list[list[ExtensionElement]],
    output_schema: TemplatedText | dict[str, Any] | None,
    input_schema: TemplatedText | dict[str, Any] | None = None,
    *,
    state_binding: StateBinding | None = None,
    tags: list[str] | None = None,
    enforce_tier: bool = True,
) -> tuple[Any, FleetResult]:
    """The reusable create path shared by the create door and the seed applier.

    Ordered name pre-checks → base rule → agent-authoring → combo/schema/bind +
    input-schema + write-validator validation → (optional) registration-tier fence
    → the locked name claim (:func:`_claim_preset_name`: store write THEN register)
    → one ``list_changed`` → the bus rebind fan-out. Returns the store record + the
    per-worker fleet report.

    EVERY door that creates a preset flows through here — the HTTP create operation, the
    in-process facet ``instance.app.presets.create`` and the declared-seed applier — so
    the fleet-wide per-name serialization the claim holds covers all three.

    ``enforce_tier`` runs the caller-authorization fence (create's door behavior); the
    seed applier passes ``False`` — a platform seed has no caller to fence and runs the
    identical content path otherwise (no logic duplicated between the two). ``tags`` labels
    version 1 in the SAME store commit (``None`` is the door's untagged create).
    """
    # The description is the bound tool's LLM-facing docstring — required non-empty on
    # every create path, so no path produces an empty-docstring preset tool.
    if not description.strip():
        raise BadRequestError("a preset description must not be empty")

    mgr = instance.app.preset_manager
    # Ordered name pre-checks (name safety + the collision cascade) and the base rule.
    await _check_create_name(name, mgr)
    await _check_create_base(base_tool, mgr)

    # When the base is an agent tool, this is an authored agent: every baked field
    # must be preset-bakeable for the agent and its baked spec must validate +
    # resolve every reference. Read through the package object so a monkeypatch bites.
    authoring_error = await _pkg._agent_authoring_error(base_tool, fixed_kwargs)
    if authoring_error is not None:
        raise BadRequestError(authoring_error)

    # Validate-before-commit: reject an unknown/illegal extension combo and a body
    # that cannot bake, BEFORE any store write, so a bad create is a 400 that never
    # persists a row.
    combo_error = _combo_registry_error(extensions)
    if combo_error is not None:
        raise BadRequestError(combo_error)
    schema_error = await _output_schema_error(base_tool, output_schema, extensions)
    if schema_error is not None:
        raise BadRequestError(schema_error)
    bind_error = await _dry_run_bind_error(
        base_tool,
        fixed_kwargs,
        name=name,
        description=description,
        output_schema=output_schema,
        input_schema=input_schema,
    )
    if bind_error is not None:
        raise BadRequestError(bind_error)
    body = PresetBody(
        base_tool=base_tool,
        description=description,
        fixed_kwargs=fixed_kwargs,
        extensions=extensions,
        output_schema=output_schema,
        input_schema=input_schema,
        state_binding=state_binding,
    )
    # An ``input_schema`` over a base tool with no registered support is a loud 400 that
    # never persists a row (never a silently-ignored schema).
    input_schema_error = _input_schema_authoring_error(body)
    if input_schema_error is not None:
        raise BadRequestError(input_schema_error)
    # The base tool's own write validator over the full body about to persist — a
    # body its base tool rejects is a 400 that never persists a row.
    write_validator_error = await _write_validator_error(body)
    if write_validator_error is not None:
        raise BadRequestError(write_validator_error)
    # Registration-tier fence: a base tool declaring ``fenced``/``secret``
    # requires the caller clears the admin fence to author a preset over it. Skipped for
    # a platform seed (``enforce_tier=False``) — no caller to fence.
    if enforce_tier:
        await _enforce_registration_tier(base_tool)

    # A preset needs the durable store; on a store-less deploy (the skeleton
    # database unconfigured) refuse cleanly here — the same predicate the
    # list / delete / reconcile paths gate on — rather than let create open Postgres
    # and fail with an opaque 500.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})

    # Attach-on-use + validate the door binding at SAVE (the write of the runnable
    # definition carrying it): its named templates are attached idempotently and its
    # expressions/adapters compiled, so a bad binding fails the create before any row.
    await _attach_body_binding(state_binding)

    record, census = await _claim_preset_name(name, body, tags)
    await instance.app.emit_list_changed("tool")
    # The fan-out waits on every sibling worker's confirmation (a fleet-sized round trip)
    # and publishes state already committed above, so it runs with the name lock
    # RELEASED — holding it here would queue every worker's create behind one confirmation
    # wait after the write it protects has landed.
    report = await fanout._fanout_reload(name, census)
    return record, report


@operation(
    summary="Create a preset",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotSupportedError],
    request_model=PresetCreate,
    response_model=PresetCreateResult,
)
async def create_preset(
    name: str,
    base_tool: str,
    description: str,
    fixed_kwargs: dict[str, Any],
    extensions: list[list[ExtensionElement]],
    output_schema: TemplatedText | dict[str, Any] | None,
    input_schema: TemplatedText | dict[str, Any] | None = None,
    state_binding: StateBinding | None = None,
) -> dict[str, Any]:
    """Create a preset, atomically.

    The shared :func:`_create_preset_core` runs the ordered name pre-checks,
    validation, store write THEN register (rolling the row fully back on a register
    failure), one ``list_changed``, and the bus rebind fan-out. The response embeds
    the per-worker fleet report under ``fanout``.

    A preset's NAME is its identity everywhere — it IS the live tool binding, and every
    reference keys on it deliberately; there are no surrogate ids.
    """
    record, report = await _create_preset_core(
        name,
        base_tool,
        description,
        fixed_kwargs,
        extensions,
        output_schema,
        input_schema,
        state_binding=state_binding,
    )
    return await _create_response(
        name,
        base_tool,
        description,
        extensions,
        output_schema,
        input_schema,
        active_version=record.active_version,
        report=report,
    )


async def _apply_seed_tool_meta(seed: PresetSeed) -> None:
    """Apply the seed's tool_meta display fields ONLY where the preset's tool_meta leaves them absent.

    A seed never overwrites an operator-set display value. ``folder_path`` is
    resolved to a leaf ``folder_id`` (creating missing folders), never a raw path
    string. A no-op when the seed declares no tool_meta.
    """
    meta = seed.tool_meta
    if meta is None:
        return
    from tai42_skeleton.operations.tool_meta import _clean_label, resolve_folder_path

    store = instance.app.tool_meta.store
    current = await store.get_meta(seed.name)
    patch: dict[str, Any] = {}
    if meta.display_name is not None and (current is None or current.display_name is None):
        # Route through the operation-door guard: a blank/whitespace display_name is refused
        # LOUDLY (not persisted as an empty label), and an accepted value is stored stripped —
        # the same invariant ``upsert_tool_meta`` enforces so ``display_name ?? name`` never
        # renders empty.
        patch["display_name"] = _clean_label(meta.display_name, "display_name")
    if meta.tags is not None and (current is None or not current.tags):
        patch["tags"] = list(meta.tags)
    if meta.folder_path is not None and (current is None or current.folder_id is None):
        folder_id = await resolve_folder_path(meta.folder_path)
        if folder_id is not None:
            patch["folder_id"] = folder_id
    if patch:
        await store.merge_meta(seed.name, patch=patch)


async def _seed_create(seed: PresetSeed) -> None:
    """Create an absent seed through the shared create core (no caller fence).

    Then apply its tool_meta where absent.
    """
    await _create_preset_core(
        seed.name,
        seed.base_tool,
        seed.description,
        seed.fixed_kwargs,
        [],
        seed.output_schema,
        seed.input_schema,
        state_binding=seed.state_binding,
        enforce_tier=False,
    )
    await _apply_seed_tool_meta(seed)


async def _apply_one_seed(seed: PresetSeed) -> None:
    """Create the seed when absent; a preset already present is left untouched.

    Idempotent across boot/reload/epoch-swap and safe under concurrent fleet boot.

    The presence check is the cheap fast path — every process of the deployment (each
    ``tai serve`` worker and the backend worker) runs this applier against ONE database, so
    a check that missed is decided by the create's own fleet-wide per-name lock
    (:func:`_claim_preset_name`), which conflicts on the sibling's committed row without
    touching its overlay.
    """
    store = instance.app.presets.store
    try:
        await store.get_preset(seed.name)
    except PresetNotFoundError:
        # A conflict is either the sibling applier / an operator's create through the HTTP
        # door claiming the name first, or a foreign (non-preset) tool of the same name.
        # Re-read the STORE to tell them apart — a preset row now present is benign and
        # idempotent, anything else is a genuine foreign-name collision and re-raises loudly.
        try:
            await _seed_create(seed)
        except ConflictError:
            present = True
            try:
                await store.get_preset(seed.name)
            except PresetNotFoundError:
                present = False
            if not present:
                raise
            logger.info("preset seeds: %r created concurrently by a sibling — treating as present", seed.name)

    # Local-load guard. A sibling's boot create lands store-side only — it does not fan out
    # to this worker at boot — so a seed present in the store may be absent from THIS worker's
    # registry. Bind the ACTIVE stored version here so every declared seed is callable on
    # first boot, before any reload. A create already registered locally, so the guard no-ops;
    # a quarantined seed stays conflicted — never force-loaded onto a base it cannot bind.
    mgr = instance.app.preset_manager
    if not mgr.is_registered(seed.name) and not mgr.is_quarantined(seed.name):
        await mgr.reload(seed.name)


async def apply_preset_seeds() -> None:
    """Startup/reload/epoch-swap handler: create every declared preset seed that is absent.

    Registered AFTER the preset-rehydrate handler so a just-created seed is LIVE in the same
    epoch (resolvable in the tool registry) — it creates through the operations-layer internal
    path, which registers the tool. Feature-OFF is legal: with the versioned store unconfigured
    every seed logs a VISIBLE skip and nothing is touched. Any real failure raises loudly.
    Idempotent across re-runs.
    """
    seeds = instance.app.presets.seeds()
    if not seeds:
        return
    if not component_store_configured(SKELETON_COMPONENT):
        for seed in seeds:
            logger.info("preset seeds: skipping seed %r — the versioned-document store is not configured", seed.name)
        return
    for seed in seeds:
        await _apply_one_seed(seed)

"""The preset register/reload engine — the skeleton ``PresetManager``.

Turns a preset (a base tool + baked ``fixed_kwargs`` + extension combos) into a
live named tool, and re-registers it whenever the runtime tool registry is
rebuilt. It owns three pieces of process-lifetime state, held in lockstep with
what is actually bound:

* an **authoritative in-memory spec map** ``name -> PresetBody`` (``base_tool``,
  ``description``, ``fixed_kwargs``, ``extensions``) — the source of
  truth for a registered preset's baked kwargs. The kernel bakes the values as
  hidden ``ArgTransform`` defaults with no readable closure, so both the tool
  face and the ephemeral-agent run path read baked kwargs FROM THIS MAP
  (:meth:`get_spec` / :meth:`baked_kwargs`), never from the tool object. A parallel
  ``name -> active_version`` map, captured in lockstep with the spec (from the same
  version-aware read), lets the run chokepoint attribute a dispatched preset with the
  version it is actually bound at (:meth:`active_version`);
* a **quarantine map** of names whose stored preset could not register at load
  (name taken by a foreign tool, or a base tool that is missing, a preset, or
  derived from a preset) to the human-readable reason — surfaced as ``conflicted``
  records carrying that reason rather than bricking boot;
* the register/reload/remove/rehydrate operations over both, plus the
  :meth:`name_conflicts` predicate the :class:`PresetStoreView` consults so a
  colliding name raises before any store write.

The engine NEVER emits ``list_changed`` — routes and startup handlers own that.
It binds/rebinds the live tool from a body it is handed (or reads the ACTIVE
store body on :meth:`reload`); persistence is the route's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from typing import TYPE_CHECKING, Any

from tai42_contract.agent import PRESET_NAME_PATTERN
from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetExistsError, PresetNameConflictError, PresetNotFoundError
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText
from tai42_kit.utils.render import SchemaBodyError, TextBodyError, resolve_schema_body

from tai42_skeleton.presets.errors import PresetStoreReadError
from tai42_skeleton.presets.maps import PresetMapError
from tai42_skeleton.tools.binding.errors import SchemaSignatureError
from tai42_skeleton.tools.binding.schema import schema_signature

if TYPE_CHECKING:
    from tai42_skeleton.app.server import TaiMCP

logger = logging.getLogger(__name__)

# The contract's preset-name law: a preset name is a live tool name AND a ``{name}`` path segment.
_PRESET_NAME_RE = re.compile(PRESET_NAME_PATTERN)

# The quarantine reason of a stored preset whose name the preset-name law refuses.
_NAME_LAW_REASON = "its name violates the preset-name law"

# The refusal of an ``output_schema`` field beside an extension entry that declares the output shape.
OUTPUT_SCHEMA_EXTENSION_CLASH = (
    "output_schema field conflicts with an explicit output-schema-declaring extension entry; "
    "declare the output shape in exactly one place"
)


def is_valid_preset_name(name: str) -> bool:
    """Return whether ``name`` is a valid preset (tool) name.

    The create route's 400 guard and :meth:`PresetManager.register`'s backstop share this rule.
    """
    return _PRESET_NAME_RE.fullmatch(name) is not None


def output_schema_extension_clash(
    extensions: Sequence[Sequence[ExtensionElement]], declares_output_schema: Callable[[ExtensionElement], bool]
) -> str | None:
    """The clash refusal when an extension entry declares the output shape a preset ``output_schema`` also sets.

    ``declares_output_schema`` answers for one entry from the live extension registry (an unknown
    entry raises there).
    """
    for combo in extensions:
        for element in combo:
            if declares_output_schema(element):
                return OUTPUT_SCHEMA_EXTENSION_CLASH
    return None


async def branch_input_schema_error(
    name: str, extensions: Sequence[Sequence[ExtensionElement]], input_schema: TemplatedText | dict[str, Any] | None
) -> str | None:
    """The refusal of an ``input_schema`` the preset's extension branches cannot present, or ``None``.

    A branch of a preset with an input schema presents that schema as named arguments, so with
    extensions the schema must be one :func:`~tai42_skeleton.tools.binding.schema.schema_signature`
    accepts. ``None`` when there are no extensions or no schema; a schema that cannot be rendered is
    refused with its own text.
    """
    if not extensions or input_schema is None:
        return None
    try:
        resolved = await resolve_schema_body(f"preset {name!r} input_schema", input_schema)
    except SchemaBodyError as exc:
        return str(exc)
    if resolved is None:
        raise AssertionError
    try:
        schema_signature(resolved)
    except SchemaSignatureError as exc:
        return f"input_schema cannot be presented to the preset's extensions: {exc}"
    return None


def preset_base_refusal(base_tool: str, origin: str | None) -> str | None:
    """The refusal of ``base_tool`` as a preset's base, given the preset it is or derives from (``origin``).

    A preset's base must be a tool that is neither a preset nor derived from one (an extension branch a
    preset produces): such a base exists only once that other preset is bound, so a preset over it would
    bind or not depending on load order. ``None`` when ``origin`` is ``None`` (no preset behind it).
    """
    if origin is None:
        return None
    if origin == base_tool:
        return f"base tool {base_tool!r} is itself a preset"
    return f"base tool {base_tool!r} is derived from preset {origin!r}"


class PresetManager:
    """Register/reload engine + authoritative spec map + quarantine map."""

    def __init__(self, app: TaiMCP) -> None:
        """Bind the manager to ``app`` and start with empty spec, version and quarantine maps."""
        self._app = app
        # name -> the spec used to BUILD the live tool; written on register,
        # dropped on teardown, rebuilt wholesale on rehydration — so it always
        # mirrors what is bound.
        self._specs: dict[str, PresetBody] = {}
        # name -> the store ACTIVE version bound as the live tool, captured TOGETHER
        # with the body it was built from (a single version-aware read) and held in
        # lockstep with ``_specs``. The run chokepoint reads it to stamp a dispatched
        # preset's ``preset-v:`` tag + root version onto the run trace, so the value
        # a run is attributed with is exactly the version currently bound.
        self._versions: dict[str, int] = {}
        # names whose stored preset could not register at load (conflicted),
        # mapped to the human-readable reason surfaced as ``conflicted_reason``.
        self._quarantine: dict[str, str] = {}
        # Per-name serialization: register/reload/remove/reconcile of ONE name
        # hold this lock across their whole teardown+re-register window so two
        # concurrent create/edit ops on the same name never interleave and clobber
        # each other. The lock is NOT reentrant, so the public methods delegate to
        # the internal UNLOCKED ``_register``/``_remove_registration``: a lock-holding
        # path (``reload``, ``reconcile_bases``) re-registers through the internal
        # form, so a path already holding a name's lock never re-acquires it. An entry
        # is created only for a VALID preset name that reaches a register/reload/
        # remove/reconcile op (name validation precedes the lock), never for a rejected
        # name; entries are not evicted on remove, so create/delete churn over distinct
        # names accretes one tiny lock each — bounded in practice by the operational
        # preset namespace, not by untrusted input.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- authoritative spec map (feeds the run path) --------------------------

    def get_spec(self, name: str) -> PresetBody:
        """Return the active spec of a REGISTERED preset.

        Raises :class:`PresetNotFoundError` if no preset by that name is registered.
        """
        try:
            return self._specs[name]
        except KeyError:
            raise PresetNotFoundError(name) from None

    def baked_kwargs(self, name: str) -> dict[str, Any]:
        """Return the active baked ``fixed_kwargs`` of a REGISTERED preset.

        The value both the tool face and the ephemeral-agent run path serve, read from the
        spec map (never the tool closure).
        """
        return self.get_spec(name).fixed_kwargs

    def is_registered(self, name: str) -> bool:
        """Whether ``name`` is a live registered preset."""
        return name in self._specs

    async def stored_preset_names(self) -> frozenset[str]:
        """The names of the stored presets; empty on a deployment with no skeleton database.

        A configured store that cannot be read raises :class:`PresetStoreReadError`, never
        an empty set.
        """
        try:
            bodies = await self._app.presets.list_active_bodies()
        except Exception as exc:
            raise PresetStoreReadError(f"the preset store could not be read: {exc}") from exc
        return frozenset(bodies)

    def active_version(self, name: str) -> int | None:
        """Return the store active version currently bound for ``name``, or ``None`` if none is registered.

        The run chokepoint reads it to attribute a dispatched preset's run with its
        ``preset-v:`` tag + root version; ``None`` for a non-preset/draft key leaves the run
        unstamped.
        """
        return self._versions.get(name)

    def registered_names(self) -> frozenset[str]:
        """Every live registered preset name."""
        return frozenset(self._specs)

    def _origin_among(self, tool_name: str, preset_names: Collection[str]) -> str | None:
        """The preset among ``preset_names`` that ``tool_name`` is, or whose extension branch it is, else ``None``.

        A branch's origin is read from the tool registry, which records the base every bound branch was
        produced from.
        """
        if tool_name in preset_names:
            return tool_name
        base = self._app.tools.base_of(tool_name)
        return base if base in preset_names else None

    def preset_origin(self, tool_name: str) -> str | None:
        """The registered preset ``tool_name`` is or derives from (one of its extension branches), else ``None``."""
        return self._origin_among(tool_name, self._specs.keys())

    def base_refusal(self, base_tool: str) -> str | None:
        """Why ``base_tool`` cannot be a preset's base because a registered preset is behind it, else ``None``.

        The base rule every door that authors a preset base shares (see :func:`preset_base_refusal`).
        """
        return preset_base_refusal(base_tool, self.preset_origin(base_tool))

    # -- quarantine map (the ``conflicted`` mechanism) ------------------------

    def is_quarantined(self, name: str) -> bool:
        """Return whether ``name`` is currently quarantined."""
        return name in self._quarantine

    def quarantined_names(self) -> frozenset[str]:
        """Return every quarantined preset name."""
        return frozenset(self._quarantine)

    def quarantine_reason(self, name: str) -> str | None:
        """Return the human-readable reason ``name`` is quarantined, or ``None`` when it is not.

        The ``conflicted_reason`` a preset row carries alongside its ``conflicted`` flag.
        """
        return self._quarantine.get(name)

    def drop_quarantine(self, name: str) -> None:
        """Remove ``name`` from the quarantine map with immediate effect.

        The single between-rehydration mutation, driven by the DELETE route's conflicted
        branch so a later create of that name starts clean.
        """
        self._quarantine.pop(name, None)

    # -- name-collision predicate (wired into the PresetStore view) -----------

    async def name_conflicts(self, name: str) -> bool:
        """Whether ``name`` collides with a LIVE non-preset base tool.

        A preset must never silently shadow a real tool. A name already owned by
        one of OUR presets is not a collision here (a duplicate is caught by the
        create route's spec-map / store-write checks); a name owned by a foreign
        registered tool is. This checks LIVE (bound) tools; a name merely REQUESTED
        by a manifest tool that is not yet bound is handled at :meth:`register`,
        which clears the stale requested entry before seeding so the preset's own
        combos still bind.
        """
        if name in self._specs:
            return False
        return name in await self._app.tools.get_tools()

    # -- register one ---------------------------------------------------------

    async def register(
        self,
        name: str,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        extensions: Sequence[Sequence[ExtensionElement]],
        description: str = "",
        output_schema: TemplatedText | dict[str, Any] | None = None,
        input_schema: TemplatedText | dict[str, Any] | None = None,
        *,
        input_map: TemplatedText | str | None = None,
        output_map: TemplatedText | str | None = None,
        state_binding: StateBinding | None = None,
        version: int = 1,
    ) -> None:
        """Bind ``name`` as a runnable tool from its full spec (public entry).

        Validates the name, takes the per-name lock, and delegates to the internal
        :meth:`_register`. A name already owned by one of OUR presets raises
        :class:`PresetExistsError`; a name held by a foreign live tool raises
        :class:`PresetNameConflictError` — the manager never silently clobbers a
        live tool or sibling preset.

        ``version`` is the store active version this spec was read at, retained beside
        the body so the run chokepoint can attribute a dispatch with it; the create
        door passes the just-created record's ``active_version`` (defaults to ``1`` —
        the version a fresh create always mints).
        """
        if not is_valid_preset_name(name):
            raise ValueError(f"invalid preset name {name!r}: must match {_PRESET_NAME_RE.pattern}")
        async with self._locks[name]:
            await self._register(
                name,
                base_tool,
                fixed_kwargs,
                extensions,
                description,
                output_schema,
                input_schema,
                input_map=input_map,
                output_map=output_map,
                state_binding=state_binding,
                version=version,
            )

    async def _register(
        self,
        name: str,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        extensions: Sequence[Sequence[ExtensionElement]],
        description: str = "",
        output_schema: TemplatedText | dict[str, Any] | None = None,
        input_schema: TemplatedText | dict[str, Any] | None = None,
        *,
        input_map: TemplatedText | str | None = None,
        output_map: TemplatedText | str | None = None,
        state_binding: StateBinding | None = None,
        version: int,
    ) -> None:
        """Bind ``name`` as a runnable tool from its full spec — the UNLOCKED core.

        Builds the baked tool through the kernel (``app.presets.bind`` —
        programmatic ``Tool.from_tool`` with each ``fixed_kwargs`` key hidden and
        fixed), seeds the preset's extension COMBOS straight into the structured
        registry in ONE call, then force-registers the prebuilt tool past manifest
        gating. All-or-nothing: a mid-bind failure tears down any partial
        registration and re-raises loudly, so the registry never holds a
        half-bound preset. It only binds the live tool — it never writes the store.

        Self-guards against a clobber: a name already in the spec map raises
        :class:`PresetExistsError`, and a name held by a foreign LIVE tool raises
        :class:`PresetNameConflictError`, before any bind. Callers that
        deliberately re-bind an existing name (reload, reconcile) tear the old
        registration down first, so the guard sees a free name.
        """
        if name in self._specs:
            raise PresetExistsError(name)
        if name in await self._app.tools.get_tools():
            raise PresetNameConflictError(name)
        tool_obj = await self._app.presets.bind(
            base_tool,
            fixed_kwargs,
            name=name,
            description=description,
            output_schema=output_schema,
            input_schema=input_schema,
            input_map=input_map,
            output_map=output_map,
        )
        self._commit_registration(
            name,
            tool_obj,
            extensions,
            base_tool=base_tool,
            fixed_kwargs=fixed_kwargs,
            description=description,
            output_schema=output_schema,
            input_schema=input_schema,
            input_map=input_map,
            output_map=output_map,
            state_binding=state_binding,
            version=version,
        )

    def _commit_registration(
        self,
        name: str,
        tool_obj: Any,
        extensions: Sequence[Sequence[ExtensionElement]],
        *,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        description: str,
        output_schema: TemplatedText | dict[str, Any] | None,
        input_schema: TemplatedText | dict[str, Any] | None,
        input_map: TemplatedText | str | None,
        output_map: TemplatedText | str | None,
        state_binding: StateBinding | None = None,
        version: int,
    ) -> None:
        """Seed the combos and force-register the prebuilt ``tool_obj``, then capture the spec and version.

        The SYNCHRONOUS tail of a register/reload.
        Holds no ``await``: the whole registry mutation lands in one loop turn, so a
        run resolving ``name`` on this loop never observes it half-bound between the
        teardown and the re-register (:meth:`reload` tears the old registration down
        immediately before calling this). All-or-nothing: a mid-register failure tears
        down the partial registration and re-raises loudly.
        """
        try:
            # Clear any stale registry entry for ``name`` first — the seed below is
            # a no-op when the name is already a requested tool, so honour that
            # "unregister-first" contract here too. Normally a no-op (a fresh preset
            # name is unknown); the one case it clears is a name requested by a
            # manifest tool whose server was down at bind time (present in the
            # registry's requested set but unbound, so it slipped past the
            # bound-only collision guard) — without this its combos would be
            # silently dropped and no branch tool would bind.
            self._app.tools.unregister_tool_info(name)
            # The combos list is the SAME shape the structured registry takes —
            # passed straight through in ONE call (no per-combo loop, no colon
            # form) so ``_tools[name]`` becomes ``[[]] + combos`` and the branch
            # tools bind off the bare runnable.
            self._app.tools.register_tool_info(name, extensions)
            self._app.tools.tool(tool_obj, force=True)
        except Exception:
            self._remove_registration(name)
            raise
        self._specs[name] = PresetBody(
            base_tool=base_tool,
            description=description,
            fixed_kwargs=fixed_kwargs,
            extensions=[list(combo) for combo in extensions],
            output_schema=output_schema,
            input_schema=input_schema,
            input_map=input_map,
            output_map=output_map,
            state_binding=state_binding,
        )
        # The version is captured in lockstep with the spec it was read at, so the two
        # never drift (both are dropped together in ``_remove_registration``).
        self._versions[name] = version

    # -- reload one (edit path) -----------------------------------------------

    async def reload(self, name: str) -> None:
        """Re-register ``name`` from its store ACTIVE version body after a ``save_version``/``rollback`` committed.

        The new body is READ and its tool BUILT while the live registration is still
        bound; only then is the old base + every branch torn down and the fresh tool
        force-registered — a teardown+register that holds NO ``await`` (see
        :meth:`_commit_registration`). This is the atomicity a concurrent run needs:
        a run resolving ``name`` on this loop (e.g. a backend worker job dispatched
        the instant a version/rollback fan-out lands) sees the OLD tool or the NEW
        tool, never a gap — a rebind never surfaces a spurious ``UnknownToolError``
        for a tool bound both before and after it.

        Never drops the live primitive: the currently-live spec is captured before
        teardown and, if the re-register raises, RESTORED so base + branches survive,
        then the error is re-raised loudly. A bind/read failure leaves the live tool
        untouched (it runs before the teardown). The already-committed store bump is
        deliberately NOT unwound — the next reload re-attempts it, and the divergence
        stays loud, never silent.

        Holds the per-name lock across the whole read+build+swap so two reloads of one
        name serialize; re-registers through the sync commit / internal
        :meth:`_register`, never the public :meth:`register`, so the held per-name lock
        is never re-acquired (the lock is not reentrant).
        """
        async with self._locks[name]:
            captured = self._specs.get(name)
            captured_version = self._versions.get(name)
            # One version-aware read: the freshly-active version and its body are
            # captured TOGETHER so the version retained beside the re-bound body is the
            # one it was actually built from (never a skewed second read). Both the read
            # and the bind run BEFORE the teardown, so the swap below yields no loop turn.
            version, body = await self._app.presets.get_active_versioned_body(name)
            tool_obj = await self._app.presets.bind(
                body.base_tool,
                body.fixed_kwargs,
                name=name,
                description=body.description,
                output_schema=body.output_schema,
                input_schema=body.input_schema,
                input_map=body.input_map,
                output_map=body.output_map,
            )
            self._remove_registration(name)
            try:
                self._commit_registration(
                    name,
                    tool_obj,
                    body.extensions,
                    base_tool=body.base_tool,
                    fixed_kwargs=body.fixed_kwargs,
                    description=body.description,
                    output_schema=body.output_schema,
                    input_schema=body.input_schema,
                    input_map=body.input_map,
                    output_map=body.output_map,
                    state_binding=body.state_binding,
                    version=version,
                )
            except Exception:
                if captured is not None:
                    await self._register(
                        name,
                        captured.base_tool,
                        captured.fixed_kwargs,
                        captured.extensions,
                        captured.description,
                        captured.output_schema,
                        captured.input_schema,
                        input_map=captured.input_map,
                        output_map=captured.output_map,
                        state_binding=captured.state_binding,
                        version=captured_version if captured_version is not None else version,
                    )
                raise

    # -- remove one (delete path) ---------------------------------------------

    async def remove(self, name: str) -> None:
        """Tear down ``name``'s base AND every branch tool, dropping its spec.

        The teardown counterpart of :meth:`register`, used by the DELETE route's
        non-conflicted branch (a conflicted record was never registered, so its
        delete is store-side only and must NOT reach here). Takes the per-name lock
        so a delete never races a concurrent register/reload of the same name.
        """
        async with self._locks[name]:
            self._remove_registration(name)

    # -- reconcile after a scoped MCP reload/deregister -----------------------

    async def reconcile_bases(self, affected_bases: set[str]) -> None:
        """Reconcile presets whose base tool is in ``affected_bases`` after a scoped MCP reload/deregister.

        Reconciles from the in-memory spec map, never the store: a preset whose base
        tool is still bound is re-registered from its spec so its ``TransformedTool``
        closure tracks the freshly-bound base instead of the stale pre-reload one; a
        preset whose base tool vanished — or that fails to re-register — is
        QUARANTINED, so its store row surfaces as ``conflicted`` rather than staying
        bound to a base that no longer exists. Each name is reconciled under its own
        lock through the internal register/teardown, so a concurrent edit of the same
        preset never interleaves.
        """
        affected = [name for name, body in self._specs.items() if body.base_tool in affected_bases]
        if not affected:
            return
        live = set(await self._app.tools.get_tools())
        for name in affected:
            async with self._locks[name]:
                body = self._specs.get(name)
                if body is None:
                    continue
                # Reconciliation re-binds the SAME stored version against a freshly-bound
                # base tool (nothing was saved), so the retained version is carried across
                # the teardown unchanged.
                version = self._versions.get(name, 1)
                self._remove_registration(name)
                if body.base_tool in live:
                    try:
                        await self._register(
                            name,
                            body.base_tool,
                            body.fixed_kwargs,
                            body.extensions,
                            body.description,
                            body.output_schema,
                            body.input_schema,
                            input_map=body.input_map,
                            output_map=body.output_map,
                            state_binding=body.state_binding,
                            version=version,
                        )
                        continue
                    except Exception:
                        reason = "re-register after MCP reload failed"
                        self._quarantine[name] = reason
                        logger.exception("preset %r quarantined: %s", name, reason)
                        continue
                reason = f"its base tool {body.base_tool!r} vanished on an MCP change"
                self._quarantine[name] = reason
                logger.error("preset %r quarantined: %s", name, reason)

    # -- rehydrate all (startup/reload hook body) -----------------------------

    async def rehydrate(self) -> None:
        """Re-register every VERSIONED preset from the store — the body a startup and reload hook run.

        ``reload_config`` wipes the whole runtime tool registry, so the spec map +
        quarantine map are cleared WHOLESALE first, then every preset is rebuilt from
        its active store body. A stale preset is QUARANTINED
        (logged loudly, surfaced as ``conflicted``) rather than registered when its
        name is taken by a foreign tool, or its base tool is missing, a stored
        preset, or derived from one (an extension branch a preset produces). A preset
        already bound as THIS SAME spec is re-adopted without a rebind (idempotent
        self-registration). One bad name never aborts the boot.

        A base absent from the live-tool snapshot is judged after every preset has
        bound, so its verdict names a preset that produces it whichever order the
        records load in.
        """
        prior = self._specs
        self._specs = {}
        self._versions = {}
        self._quarantine = {}
        # One batched version-aware read: each preset's active version and body are
        # captured TOGETHER, so the version the engine retains beside a body is exactly
        # the one it was built from.
        versioned = await self._app.presets.list_active_versioned_bodies()
        preset_names = set(versioned)
        # Snapshot the live tools ONCE, before registering any preset, so a name
        # found here that is NOT one of ours is genuinely a foreign tool.
        live = set(await self._app.tools.get_tools())
        # name -> base tool, for each preset whose base was absent from the snapshot.
        absent_bases: dict[str, str] = {}
        for name, (version, body) in versioned.items():
            await self._rehydrate_one(name, version, body, prior, preset_names, live, absent_bases)
        for name, base in absent_bases.items():
            refusal = preset_base_refusal(base, self._origin_among(base, preset_names))
            reason = f"its {refusal}" if refusal is not None else f"its base tool {base!r} is not a registered tool"
            self._quarantine[name] = reason
            logger.error("preset %r quarantined: %s", name, reason)

    async def _rehydrate_one(
        self,
        name: str,
        version: int,
        body: PresetBody,
        prior: dict[str, PresetBody],
        preset_names: set[str],
        live: set[str],
        absent_bases: dict[str, str],
    ) -> None:
        if not is_valid_preset_name(name):
            self._quarantine[name] = _NAME_LAW_REASON
            logger.error("preset %r quarantined: %s", name, _NAME_LAW_REASON)
            return
        bound = name in live
        ours = name in prior
        if bound and not ours:
            reason = "its name is occupied by an existing tool"
            self._quarantine[name] = reason
            logger.error("preset %r quarantined: %s", name, reason)
            return
        if bound and ours and prior[name] == body:
            # Already registered as this exact preset — re-adopt the spec + its version
            # without a rebind (the tool + its branches are untouched).
            self._specs[name] = body
            self._versions[name] = version
            return
        if bound:
            # Ours but its active body changed — tear the stale registration down
            # before re-binding the new body.
            self._remove_registration(name)
        base = body.base_tool
        refusal = preset_base_refusal(base, self._origin_among(base, preset_names))
        if refusal is not None:
            reason = f"its {refusal}"
            self._quarantine[name] = reason
            logger.error("preset %r quarantined: %s", name, reason)
            return
        if base not in live:
            # Judged by :meth:`rehydrate` once every preset has bound.
            absent_bases[name] = base
            return
        try:
            reason = await self._bind_rehydrated(name, version, body)
        except Exception:
            reason = "registration failed"
            self._quarantine[name] = reason
            logger.exception("preset %r quarantined: %s", name, reason)
            return
        if reason is not None:
            self._quarantine[name] = reason
            logger.error("preset %r quarantined: %s", name, reason)

    async def _bind_rehydrated(self, name: str, version: int, body: PresetBody) -> str | None:
        """Bind a stored ``body`` whose base is live; the reason it is refused, or ``None`` once bound.

        A body the extensions cannot present, or whose maps or text bodies are refused, is
        refused with that reason; any other failure raises.
        """
        try:
            clash = (
                output_schema_extension_clash(body.extensions, self._app._extension_registry.declares_output_schema)
                if body.output_schema is not None
                else None
            )
            if clash is None:
                clash = await branch_input_schema_error(name, body.extensions, body.input_schema)
            if clash is not None:
                return clash
            await self._register(
                name,
                body.base_tool,
                body.fixed_kwargs,
                body.extensions,
                body.description,
                body.output_schema,
                body.input_schema,
                input_map=body.input_map,
                output_map=body.output_map,
                state_binding=body.state_binding,
                version=version,
            )
        except (PresetMapError, TextBodyError) as exc:
            return str(exc)
        return None

    # -- internal teardown ----------------------------------------------------

    def _remove_registration(self, name: str) -> None:
        """Drop ``name``'s base and branch tools, its structured-registry entry, and its spec.

        The shared teardown for reload, remove, and register rollback.
        """
        branches = self._app.tools.unregister_tool_base(name)
        for branch in branches:
            self._safe_remove(branch)
        self._safe_remove(name)
        self._specs.pop(name, None)
        self._versions.pop(name, None)

    def _safe_remove(self, name: str) -> None:
        """Remove a bound tool, tolerating a name that was never bound.

        Only ``KeyError`` (the FastMCP provider's "no such tool" for an
        idempotent teardown / a partially-bound register rollback) is tolerated;
        every other failure propagates.
        """
        with contextlib.suppress(KeyError):
            self._app.tools.remove_tool(name)

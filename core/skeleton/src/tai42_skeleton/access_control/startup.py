"""Startup checks for access control.

The policy RULES live in Postgres and the live-context/version-counter surfaces are
plain Redis reads that fail closed at request time, so neither needs a boot probe.
What DOES get boot-time treatment lives here: the gate state is handed to the kit in
every epoch build, and, when access control is enabled, the configured identity
providers' OWN storage is probed; the roles the
control plane hands out are seeded; the pre-auth surface is enumerated and guarded
against an authenticated route on it; and a registered accounts provider left
out of the resolution chain fails the boot rather than minting dead sessions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tai42_kit.access_control.registry import get_identity_provider_factory_staged
from tai42_kit.accounts.registry import iter_accounts_provider_factories_staged
from tai42_kit.utils.worker_secret_capability import set_access_control_gate_state

from tai42_skeleton.access_control.path_canon import MalformedPathError, canonicalize_path, under_prefix
from tai42_skeleton.access_control.settings import access_control_settings

logger = logging.getLogger(__name__)


def declare_gate_state_to_kit() -> None:
    """Hand the access-control gate state to the kit, which stamps it onto every enqueued callback job.

    Run first in every epoch build, so the generation the build assembles carries the gate
    state its own settings resolve.
    """
    set_access_control_gate_state(access_control_settings().enable)


async def probe_identity_provider() -> None:
    """Instantiate EVERY configured identity provider ONCE, record it on the epoch, and probe its own storage.

    This is the per-epoch eager-instantiation the live verifier and the provider's login
    routes both resolve against. Resolves each name in ``auth_providers`` through the
    STAGED identity registry (the generation THIS build assembled), instantiates the
    provider once against the access-control settings (whose ``admin`` services the
    freshly-built AuthAdapter has already installed), records it through
    ``app.record_auth_provider`` so a later request never re-instantiates it nor reads a
    plugin module holder, then awaits
    its ``healthcheck()``. A provider whose storage needs no boot probe inherits the
    contract's default no-op; a key-minting provider probes its own record store. ANY
    provider's failure propagates, so a deployment against a backend a provider cannot
    use fails LOUDLY at build time rather than on the first authenticated request. The
    build populates the epoch under construction; a failed build discards it whole.
    """
    from tai42_skeleton.app.instance import app

    settings = access_control_settings()
    chain = settings.resolved_auth_providers()
    if not chain:
        raise RuntimeError(
            "access_control: the gate is enabled but no identity provider is registered — the "
            "resolved auth-provider chain is empty, so no credential could ever authenticate. "
            "Register an identity provider in the manifest, or set ACCESS_CONTROL_AUTH_PROVIDERS"
        )
    for name in chain:
        provider = get_identity_provider_factory_staged(name)(settings)
        app.record_auth_provider(name, provider)
        await provider.healthcheck()


async def seed_roles() -> None:
    """Seed the default role templates (admin/editor/viewer) at startup whenever access control is enabled.

    Idempotent create-only: an operator-edited template is never overwritten. Runs
    before the server accepts traffic so a bootstrap ``apply_role(user_id, "admin")``
    can never ``KeyError`` on a fresh deployment. A seeding failure fails the boot
    loudly, the same posture as the provider probe.

    The templates live in the versioned document store, so seeding is skipped when no
    versioned store is configured — the same store-configured gate the versioned-preset
    rehydration handler applies, so an access-control deployment without a versioned
    store never opens a Postgres connection at boot.
    """
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.db import SKELETON_COMPONENT

    if not component_store_configured(SKELETON_COMPONENT):
        return
    from tai42_skeleton.access_control.roles import seed_default_roles

    await seed_default_roles()


async def check_route_rows_canonical() -> None:
    """Refuse to boot on a route table holding a row whose url is not in its canonical form.

    Every request is looked up by its canonical path, so a non-canonical row is a mapping no
    request can reach — and one its own remove/unpin could not address. The fix is a reset of
    the access-control store; the rows are never rewritten here. Gated like
    :func:`seed_roles` — skipped when no skeleton store is configured.
    """
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.access_control import management
    from tai42_skeleton.db import SKELETON_COMPONENT

    if not component_store_configured(SKELETON_COMPONENT):
        return
    stranded: list[str] = []
    for url in await management.get_all_route_mappings():
        try:
            canonical = canonicalize_path(url)
        except MalformedPathError:
            stranded.append(url)
            continue
        if canonical != url:
            stranded.append(url)
    if stranded:
        raise RuntimeError(
            f"access_control: the route table holds non-canonical rows: {sorted(stranded)} — reset the "
            "access-control store (the canonical form decodes each segment once, collapses slashes and dot "
            "segments, and drops a trailing slash)"
        )


async def check_always_public_routes() -> None:
    """Enumerate the pre-auth surface and refuse an authenticated route on it.

    After routes are registered, walk the route registry: every route declaring
    ``pre_auth=True`` and every route under an operator's ``always_public_path_prefixes`` is
    named in ONE info line (so the surface whose presented credentials are never verified is
    VISIBLE at every boot). The boot FAILS CLOSED — raises — when a route under an
    always-public prefix carries ``authed=True`` (it resolves public at runtime yet declares
    itself authed: a credential-front-door contradiction), or when a route declaring
    ``pre_auth=True`` is not public (registration refuses it; nothing inconsistent may reach
    the registry by any path).
    """
    from tai42_skeleton.app.route_registry import route_registry

    settings = access_control_settings()
    prefixes = settings.always_public_path_prefixes

    public_routes = []
    authed_offenders = []
    pre_auth_offenders = []
    for meta in route_registry.routes():
        under_operator_prefix = _under_prefixes(meta.path, prefixes)
        if meta.pre_auth and not meta.public:
            pre_auth_offenders.append(meta.path)
        if not (under_operator_prefix or meta.pre_auth):
            continue
        public_routes.extend(f"{method} {meta.path}" for method in meta.methods)
        if under_operator_prefix and meta.authed:
            authed_offenders.append(meta.path)

    if pre_auth_offenders:
        raise RuntimeError(
            "access_control: routes declare pre_auth=True but are authenticated — a pre-auth surface "
            f"must be public: {sorted(set(pre_auth_offenders))}"
        )
    if authed_offenders:
        raise RuntimeError(
            "access_control: routes under an always-public prefix declare authed=True — a public "
            f"route must not declare itself authed: {sorted(set(authed_offenders))}"
        )

    if public_routes:
        logger.info("access_control: pre-auth routes (no auth): %s", ", ".join(sorted(public_routes)))


async def check_spa_shell_public() -> None:
    """Audit the route surface outside the control plane — every method — against the public-admission rules.

    Driven by the ROUTE REGISTRY, never a static list, and EXHAUSTIVE: it iterates EVERY
    registered handler route outside the control plane (``RouteRegistry.control_plane_prefixes``:
    ``/api`` and the mounted streamable-http transport) — CONCRETE AND TEMPLATED, on EVERY
    method — and never silently skips one. Each must fall into exactly ONE bucket, or the
    boot FAILS closed:

    * consciously ACKNOWLEDGED public — its REGISTERED path (the template string for a
      templated route) is in ``acknowledged_public_routes``. This is a registry-level
      match: templates are ordinary keys. The operational probes, the webhook and trigger
      ingress doors, and the SPA shell catch-all itself are the app's acknowledged public
      routes;
    * ``authed=True`` and visible to the GET-only shell fallback's derivation — a CONCRETE
      authed GET route is in the DERIVED reserved set (the shell fallback skips it). A
      TEMPLATED ``authed=True`` GET route is structurally NOT derivable into the concrete
      reserved set: the verifier's declared-protection tier now resolves such a route
      (matched per method) to the universal scope BEFORE the shell fallback could fire, so
      it is protected at runtime — but the boot still FAILS it (the author must
      ``/api``-prefix it — control-plane excluded — or consciously acknowledge it) so the
      shell fallback's concrete-only derivation is never the SOLE thing standing between a
      templated authed GET and the public shell, the same belt-and-braces posture as the
      always-public authed refusal. An authed route that serves NO GET passes: the GET-only
      shell can never reach it, and the declared-protection tier protects it at runtime;
    * ``authed=False`` and NOT acknowledged → the boot FAILS on ANY method: a boot-log flag
      is not a control; a publicly declared non-API door — a public GET page or a public
      POST ingress — must be a consciously reviewed decision. This is what keeps the gate
      from weakening when the verifier's declared-public tier opens public POST doors: such
      a door exists only after a reviewer acknowledged it.

    Control-plane routes (concrete and templated alike) are excluded: the SPA catch-all never
    matches them, so the shell tier can never reach them regardless of auth or templating. A
    declared-public ``/api`` route is granted by the verifier's owner-agnostic declared-public
    tier from its ``authed=False`` registration, never by this audit. An
    ``acknowledged_public_routes`` entry under a control-plane prefix FAILS the boot: an
    acknowledged PUBLIC control-plane route is a contradiction.
    The fallback state and the derived + acknowledged surfaces are printed so drift and the
    public-by-declaration vs shell-fallback split stay reviewable in ops logs. The audit's
    exhaustiveness is what lets the runtime fallback (concrete-match only) rely on it for
    templated routes rather than build a second matcher — see ``resolve_resource_ids``.

    Finally it confirms the terminal-deny exclusion: a probe under each control-plane prefix
    is excluded from the shell tier, so the GET fallback can never open the control plane.
    (Exercised end-to-end by the terminal-deny + route-walk tests.)
    """
    from tai42_skeleton.access_control.path_canon import under_prefix
    from tai42_skeleton.access_control.verifier import registered_reserved_get_paths, under_control_plane
    from tai42_skeleton.app.route_registry import route_registry

    settings = access_control_settings()
    control_plane = route_registry.control_plane_prefixes()
    for entry in settings.acknowledged_public_routes:
        for prefix in control_plane:
            if under_prefix(entry, prefix):
                raise RuntimeError(
                    f"access_control: acknowledged_public_routes entry {entry!r} is under the control-plane "
                    f"prefix {prefix!r} — an acknowledged PUBLIC control-plane route is a contradiction"
                )
    derived = registered_reserved_get_paths()
    acknowledged = frozenset(settings.acknowledged_public_routes)

    logger.info(
        "access_control: SPA-shell public fallback %s; derived reserved (gated) non-/api GET routes: %s",
        "ON" if settings.spa_shell_public else "OFF",
        ", ".join(sorted(derived)) or "(none)",
    )

    audit = _classify_spa_shell_routes(acknowledged, derived)
    _assert_spa_shell_audit_clean(audit)

    if audit.acknowledged_present:
        logger.info(
            "access_control: acknowledged public-by-declaration non-/api GET routes: %s",
            ", ".join(sorted(set(audit.acknowledged_present))),
        )

    # Terminal-deny confirmation: the resolver structurally excludes the control plane
    # from the shell tier, so no unmatched control-plane path can ever reach the SPA shell.
    for prefix in control_plane:
        probe = f"{prefix}/__boot_probe__"
        if not under_control_plane(probe):
            raise RuntimeError(
                f"access_control: control-plane probe {probe!r} is not excluded from the SPA-shell tier — "
                "the terminal-deny invariant is broken"
            )


@dataclass
class _SpaShellAudit:
    """The four buckets every registered handler route outside the control plane is sorted into by the audit.

    Consciously acknowledged, acknowledged-yet-authed (a contradiction),
    authed-but-invisible-to-the-fallback, and public-by-declaration-yet-unacknowledged.
    """

    acknowledged_present: list[str] = field(default_factory=list)
    acknowledged_but_authed: list[str] = field(default_factory=list)
    invisible_authed: list[str] = field(default_factory=list)
    unacknowledged: list[str] = field(default_factory=list)


def _classify_spa_shell_routes(acknowledged: frozenset[str], derived: frozenset[str]) -> _SpaShellAudit:
    """Bucket every registered non-mounted handler route outside the control plane (every method).

    See :func:`check_spa_shell_public` for the rule each bucket encodes.
    """
    from tai42_skeleton.access_control.path_canon import canonicalize_path
    from tai42_skeleton.access_control.verifier import under_control_plane
    from tai42_skeleton.app.route_registry import route_registry

    audit = _SpaShellAudit()
    for meta in route_registry.routes():
        # A MOUNTED surface (an MCP transport, the sub-MCP mount) is never served by the
        # SPA shell — the mount matches first and answers behind its own credential gate
        # — so it is outside this audit's subject: the handler route surface.
        if meta.mounted:
            continue
        registered = meta.path
        # The control plane is excluded structurally: the SPA catch-all does not match it (a
        # SpaFallbackRoute), so the shell tier never reaches it. The literal REGISTERED prefix
        # decides (registered paths carry clean, un-encoded prefixes), so a templated /api
        # route is excluded too.
        if under_control_plane(registered):
            continue
        templated = "{" in registered
        # Acknowledgment is REGISTRY-LEVEL: the REGISTERED path — the template string for a
        # templated route — is an ordinary key compared against acknowledged_public_routes.
        # A consciously-public route passes here whatever its concrete/templated shape.
        if registered in acknowledged:
            audit.acknowledged_present.append(registered)
            # An acknowledged route is served public at runtime (resolve_resource_ids grants
            # it the public resource id), so an authed=True declaration on the same route is a
            # contradiction: the acknowledgment silently strips its gate. Refuse boot rather
            # than let the operator believe the route is protected.
            if meta.authed:
                audit.acknowledged_but_authed.append(registered)
            continue
        if meta.authed:
            # The invisible-to-the-derivation bucket guards the GET-only SPA-shell fallback,
            # so it applies to GET-serving routes alone: a route that serves no GET can never
            # reach the shell, and the declared-protection tier protects it at runtime. For a
            # GET-serving authed route it is gated ONLY if the fallback derivation can SEE it.
            # A CONCRETE authed route is in the derived reserved set (the shell skips it). A
            # TEMPLATED authed route is structurally not derivable; the declared-protection
            # tier now protects such a route at runtime, but the boot still refuses it so the
            # shell fallback's concrete-only derivation is never the sole protection
            # (belt-and-braces).
            if "GET" in meta.methods and (templated or canonicalize_path(registered) not in derived):
                audit.invisible_authed.append(registered)
        else:
            # authed=False and not acknowledged: public by declaration with no conscious
            # review — on ANY method. A public POST ingress door exists only after a reviewer
            # acknowledged it, the same conscious step the public GET doors already require.
            audit.unacknowledged.append(registered)
    return audit


def _assert_spa_shell_audit_clean(audit: _SpaShellAudit) -> None:
    """Fail the boot closed on any non-clean SPA-shell bucket."""
    if audit.acknowledged_but_authed:
        raise RuntimeError(
            "access_control: acknowledged_public_routes names authed=True registered route(s): "
            f"{sorted(set(audit.acknowledged_but_authed))} — an acknowledged route is served public at runtime "
            "(the resolver grants it the public resource id), so a gated authed=True declaration is "
            "contradictory and would be silently stripped: either remove it from "
            "ACCESS_CONTROL_ACKNOWLEDGED_PUBLIC_ROUTES or set authed=False on the route"
        )
    if audit.invisible_authed:
        raise RuntimeError(
            "access_control: authed=True non-/api GET route(s) would be served the public SPA shell — they are "
            f"not visible in the derived reserved set: {sorted(set(audit.invisible_authed))}. A concrete route must "
            "register so it joins the derived set; a TEMPLATED route is structurally not derivable, so /api-prefix "
            "it (control-plane excluded) or add its registered template to ACCESS_CONTROL_ACKNOWLEDGED_PUBLIC_ROUTES "
            "if it is genuinely public"
        )
    if audit.unacknowledged:
        raise RuntimeError(
            "access_control: authed=False non-/api GET route(s) are public by declaration but not acknowledged: "
            f"{sorted(set(audit.unacknowledged))} — add each (the registered path, or the template string for a "
            "templated route) to ACCESS_CONTROL_ACKNOWLEDGED_PUBLIC_ROUTES if it is intentionally public, else set "
            "authed=True or remove the route"
        )


async def check_route_actions() -> None:
    """Boot-fail on a route whose authorization action-class cannot be resolved.

    Every gated route carries a required action-class (``read``/``write``/``fenced``/
    ``secret``) — the SINGLE source of its authorization character. A route the registry
    cannot classify, or a grantable ``read``/``write`` route whose declared class
    disagrees with its HTTP method, is a fail-closed contradiction the boot REFUSES
    (allow-by-omission is dead), mirroring the ``summary``/``tags`` registration raises.
    Runs after the routers register so the whole surface is audited at once.
    """
    from tai42_skeleton.app.route_registry import route_action_violations

    violations = route_action_violations()
    if violations:
        raise RuntimeError(
            "access_control: gated route(s) failed the action-class audit — every gated route must "
            f"resolve to a read/write/fenced/secret action-class: {sorted(violations)}"
        )


async def check_fenced_routes_resolvable() -> None:
    """Fail the boot — and every in-place reload — if a registered fenced/secret route does not resolve to itself.

    The admin-only fence is enforced ONLY where ``resolve_route_meta`` returns the route:
    a genuinely-unregistered path resolves to ``None`` and the per-tag gate correctly
    does not act on it, but a REGISTERED fenced/secret route that fails to resolve would
    be a SILENT fail-open — the fence would never fire. This check closes that by
    construction: every ``fenced``/``secret`` route must resolve via
    ``resolve_route_meta(path, method)`` to ITSELF for each of its methods, or the run
    refuses to proceed. Wired as both a startup and a reload handler, so a reload that
    mounts a fenced route resolving elsewhere (or nowhere) fails the reload op loudly
    rather than serving that route past its fence until a restart. Runs after the routers
    register so the whole surface is audited.

    The resolver's route index is rebuilt first so it reflects the routes that just
    registered — the audit then validates the live surface, and leaves the runtime index
    consistent with what it verified rather than trusting a possibly-stale earlier build.

    Enumerates through ``load_all_routes`` so the enumeration universe is imported before
    the audit runs — in this started process that is the deployment's served router
    surface, so the fence guarantee is verified against exactly what the deployment serves;
    iterating the raw registry could pass VACUOUSLY (an empty loop verifies nothing) had the
    routers not yet been imported.
    """
    from tai42_skeleton.access_control.role_gate import reset_route_index, resolve_route_meta
    from tai42_skeleton.app.route_registry import load_all_routes

    reset_route_index()
    unresolvable: list[str] = []
    for meta in load_all_routes():
        if meta.action not in ("fenced", "secret"):
            continue
        unresolvable.extend(
            f"{method} {meta.path}" for method in meta.methods if resolve_route_meta(meta.path, method) is not meta
        )

    if unresolvable:
        raise RuntimeError(
            "access_control: fenced/secret route(s) do not resolve back to themselves via resolve_route_meta — "
            f"the admin-only fence would silently fail open for: {sorted(unresolvable)}"
        )


async def check_raw_path_routes_resolvable() -> None:
    """Fail the boot — and every reload — if an encoded-slash raw-path-matched route does not resolve to itself.

    A record ``{key}`` legitimately carries ``/`` (a thread key), sent as one ``%2F``
    segment; ``HttpSurface.use_raw_path_key`` marks these routes raw-path-matched so the
    resolver keeps the encoded slash to one segment and resolves them to their protected
    resource. An unmarked route is fail-CLOSED (an encoded slash outside a marked route is
    refused as malformed, and no ``/api`` path resolves to the public catch-all), so this
    check audits the other direction: every route that IS marked must still resolve, via
    ``resolve_route_meta``, back to ITSELF under an encoded-slash probe — else the marked
    door would refuse the exact keys it exists to serve, and the run refuses to proceed.
    Wired as both a startup and a reload handler; runs after the routers register so the
    whole marked surface is audited (a raw-path family re-marked each epoch).
    """
    import re

    from tai42_skeleton.access_control.path_canon import MalformedPathError
    from tai42_skeleton.access_control.role_gate import reset_route_index, resolve_route_meta
    from tai42_skeleton.app.route_registry import load_all_routes

    def _probe(template: str) -> str:
        # A plain ``{name}`` becomes an ENCODED-slash value (exercises the raw-path fence);
        # a ``{name:path}`` becomes a plain multi-segment value.
        return re.sub(r"\{[^}]+\}", lambda m: "a/b" if ":path" in m.group(0) else "a%2Fb", template)

    reset_route_index()
    unresolvable: list[str] = []
    for meta in load_all_routes():
        if not meta.raw_path_matched:
            continue
        probe = _probe(meta.path)
        for method in meta.methods:
            # A raise means the encoded slash fenced off a route the mark no longer covers:
            # the condition audited here, so it counts as unresolvable rather than propagating.
            try:
                resolved = resolve_route_meta(probe, method)
            except MalformedPathError:
                resolved = None
            if resolved is not meta:
                unresolvable.append(f"{method} {meta.path}")

    if unresolvable:
        raise RuntimeError(
            "access_control: raw-path-matched route(s) do not resolve back to themselves under an encoded "
            "slash — the resolver would refuse the keys these doors exist to serve, "
            f"for: {sorted(unresolvable)}"
        )


async def check_accounts_providers_configured() -> None:
    """Refuse to boot when a registered accounts provider is left out of the chain.

    A registered accounts provider still advertises its login methods and mints
    sessions, but if it is missing from ``auth_providers`` the verifier chain never
    consults it — every minted session then 401s as a clean "unknown token" with
    nothing logging the cause. That is misconfiguration, not a legal state, so the boot
    fails loudly naming the missing providers and the fix.
    """
    settings = access_control_settings()
    configured = set(settings.resolved_auth_providers())
    # Read the STAGED generation: this boot check keys on the providers THIS build
    # registered, so a reload validates the generation it is assembling.
    missing = [name for name, _factory in iter_accounts_provider_factories_staged() if name not in configured]
    if missing:
        raise RuntimeError(
            "access_control: registered accounts provider(s) are missing from the resolution chain: "
            f"{missing} — add them to ACCESS_CONTROL_AUTH_PROVIDERS or their minted sessions will never "
            "authenticate"
        )


def _under_prefixes(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(under_prefix(path, prefix) for prefix in prefixes)

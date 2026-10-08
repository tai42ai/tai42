"""Coordinated staged-registration lifecycle for every per-generation global.

An epoch build stages each code-populated global — the connector, identity, accounts,
and operation registries, plus the monitoring backend, the Studio plugin registry, the
route registry's ``/api`` shape index, and the access-control gate state declared to the
kit — into a fresh generation off to the side, and promotes them ALL TOGETHER only if the
build succeeds.
A failed build drops every staged generation untouched, so the live epoch keeps serving
against a complete, unmutated set of globals.

The identity and accounts registries live in the kit, where plugins register into them
without an app handle; every one of these globals stands on the kit's staged-generation
primitives, and this skeleton helper is the ONE place their staging is driven in
lockstep. ``begin`` runs after ``reset_all_settings`` in the build+swap primitive;
``commit`` runs in the no-await swap stretch; ``abort`` runs on the failure branch. The
order within each phase is immaterial — each global's promotion is an independent atomic
reference assignment.
"""

from __future__ import annotations

from tai42_kit.access_control import registry as identity_registry
from tai42_kit.accounts import registry as accounts_registry
from tai42_kit.utils import worker_secret_capability as gate_state

from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.connectors.providers import registry as connector_registry
from tai42_skeleton.monitoring import registry as monitoring_registry
from tai42_skeleton.operations.registry import operation_registry
from tai42_skeleton.plugins import registry as studio_registry


def begin_staging_all() -> None:
    """Open a fresh staged generation for every per-generation global.

    Each committed generation serving the live epoch is left untouched.
    """
    connector_registry.begin_staging()
    identity_registry.begin_staging()
    accounts_registry.begin_staging()
    operation_registry.begin_staging()
    monitoring_registry.begin_staging()
    studio_registry.begin_staging()
    route_registry.begin_shape_staging()
    gate_state.begin_staging()


def commit_staging_all() -> None:
    """Promote every staged generation to committed — one atomic reference assignment each.

    Runs in the primitive's no-await swap stretch, so the whole set flips before any request
    can observe a mix of generations.
    """
    connector_registry.commit_staging()
    identity_registry.commit_staging()
    accounts_registry.commit_staging()
    operation_registry.commit_staging()
    monitoring_registry.commit_staging()
    studio_registry.commit_staging()
    route_registry.commit_shape_staging()
    gate_state.commit_staging()


def abort_staging_all() -> None:
    """Drop every staged generation on a failed build.

    No committed global is touched, so the live epoch keeps serving exactly what it served
    before.
    """
    connector_registry.abort_staging()
    identity_registry.abort_staging()
    accounts_registry.abort_staging()
    operation_registry.abort_staging()
    monitoring_registry.abort_staging()
    studio_registry.abort_staging()
    route_registry.abort_shape_staging()
    gate_state.abort_staging()

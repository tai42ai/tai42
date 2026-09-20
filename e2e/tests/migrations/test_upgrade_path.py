"""The deploy/upgrade path: every runner migrates before it serves.

A fresh deploy of the framework runs its pending migrations before any process
serves, so the boot gate (which refuses a database with pending migrations) never
fires in normal operation. Two runners encode that, and this leg asserts each from
its statically rendered artifact — no live cluster needed:

- *chart*: the schema-init hook Job runs ``tai db migrate``. On external Postgres
  it is a pre-install/pre-upgrade hook (schema lands before serve/backend pods); on
  the quickstart Postgres it is a post-install/post-upgrade hook.
- *compose*: the one-shot ``db-migrate`` service runs ``db migrate`` and serve +
  backend gate on its successful completion.

- *upgrade replay*: parametrized over the released framework versions to replay
  from. Empty until a release exists — old->new replay only has meaning from the
  second framework release (see ``PREVIOUS_FRAMEWORK_VERSIONS``); the leg extends
  as versions accrue, with no other change.

The artifacts live in the tai-distribution checkout and render through
``helm`` / ``docker compose``; a missing checkout or CLI is an env-limited skip.
"""

from __future__ import annotations

import pytest

from ._upgrade_path_support import (
    GATE_CONDITION,
    MIGRATE_ARGS,
    PREVIOUS_FRAMEWORK_VERSIONS,
    compose_config,
    distribution_root,
    released_chart_dir,
    render_schema_init_job,
    require_cli,
)


def _schema_init_container(job: dict) -> dict:
    """The schema-init container of a rendered Job, by name — never positional, so
    an added sidecar can never silently shift which container is asserted."""
    containers = job["spec"]["template"]["spec"]["containers"]
    named = {c["name"]: c for c in containers}
    assert "schema-init" in named, f"no schema-init container in {sorted(named)}"
    return named["schema-init"]


@pytest.mark.parametrize(
    ("quickstart", "expected_hook"),
    [
        (False, "pre-install,pre-upgrade"),
        (True, "post-install,post-upgrade"),
    ],
    ids=["external-pg", "quickstart-pg"],
)
def test_chart_schema_init_hook_migrates_before_serve(quickstart: bool, expected_hook: str) -> None:
    require_cli("helm", renders="chart schema-init hook")
    root = distribution_root()

    job = render_schema_init_job(root / "charts" / "tai", quickstart=quickstart)

    assert job["kind"] == "Job"
    # The hook runs the migration chain, not the retired one-shot apply.
    assert _schema_init_container(job)["args"] == MIGRATE_ARGS
    # The hook PHASE is what makes it migrate-before-serve: pre-hook on external
    # Postgres (schema before pods), post-hook on the release-owned quickstart DB.
    assert job["metadata"]["annotations"]["helm.sh/hook"] == expected_hook


def test_compose_bundle_migrates_before_serve() -> None:
    require_cli("docker", renders="compose bundle")
    root = distribution_root()

    services = compose_config(root, overlay=False)["services"]

    # The one-shot runs the migration chain and exits (never a long-lived service).
    db_migrate = services["db-migrate"]
    assert db_migrate["command"] == MIGRATE_ARGS
    assert db_migrate.get("restart") == "no"

    # Both serving processes refuse to start until the migrate one-shot exits 0 —
    # migrate-before-serve, encoded as a completion gate.
    for name in ("serve", "backend"):
        gate = services[name]["depends_on"]["db-migrate"]
        assert gate["condition"] == GATE_CONDITION, f"{name} does not gate on db-migrate completion"


def test_compose_local_overlay_keeps_migrate_service() -> None:
    # The local build overlay is keyed by service name; a rename that orphaned the
    # migrate service would drop its build override and its completion gate. Assert
    # the overlaid bundle still resolves with the gated one-shot intact.
    require_cli("docker", renders="compose bundle")
    root = distribution_root()

    services = compose_config(root, overlay=True)["services"]

    assert services["db-migrate"]["command"] == MIGRATE_ARGS
    assert "build" in services["db-migrate"], "local overlay lost the db-migrate build override"
    for name in ("serve", "backend"):
        assert services[name]["depends_on"]["db-migrate"]["condition"] == GATE_CONDITION


@pytest.mark.parametrize(
    "base_version",
    PREVIOUS_FRAMEWORK_VERSIONS
    or [
        pytest.param(
            None,
            marks=pytest.mark.skip(
                reason="old->new deploy replay activates from the second framework release; "
                "no prior release is pinned in PREVIOUS_FRAMEWORK_VERSIONS yet"
            ),
        )
    ],
)
def test_upgrade_replays_previous_framework_release(base_version: str) -> None:
    # Render the schema-init hook of the released base version beside the current
    # one: the upgrade path is only safe if the migrate hook gated the OLD deploy
    # too, so the database is never served with a pending chain across the upgrade.
    require_cli("helm", renders="chart schema-init hook")
    root = distribution_root()

    old = _schema_init_container(render_schema_init_job(released_chart_dir(base_version), quickstart=False))
    new = _schema_init_container(render_schema_init_job(root / "charts" / "tai", quickstart=False))

    assert old["args"] == MIGRATE_ARGS, f"released {base_version} chart did not migrate before serve"
    assert new["args"] == MIGRATE_ARGS

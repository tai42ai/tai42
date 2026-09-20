"""Support for the deploy/upgrade-path leg: the distribution bundle and its two
migrate-before-serve surfaces, rendered without a live cluster.

A fresh deploy must never serve against a database with pending migrations. Two
runners encode that ordering, and this module renders each statically so the leg
can assert it:

  - the chart schema-init hook (``helm template``): a Job running ``tai db
    migrate``. On EXTERNAL Postgres it is a ``pre-install,pre-upgrade`` hook, so
    the schema lands before any serve/backend pod starts; on the QUICKSTART
    Postgres it is ``post-install,post-upgrade`` (the release DB cannot exist at
    pre-hook time) and the boot gate self-heals the window.
  - the compose bundle (``docker compose config``): a one-shot ``db-migrate``
    service running ``db migrate``, which serve and backend gate on via
    ``depends_on: {condition: service_completed_successfully}``.

The artifacts live in the ``tai-distribution`` checkout (outside the monorepo),
and rendering needs ``helm`` / ``docker`` on PATH. A missing checkout or CLI is an
env-limited skip with the hint to supply it — never a silent pass. Wrong rendered
content is a hard assertion failure in the leg itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

# The distribution checkout holding the chart + compose bundle. Explicit override
# wins; otherwise the sibling checkout beside the monorepo members (the layout the
# release/deploy tooling assumes).
_DISTRIBUTION_ENV = "TAI_E2E_DISTRIBUTION_PATH"
# A local archive of PREVIOUSLY released chart trees, one ``<version>/`` subdir per
# release, consulted only by the old->new replay leg. Absent in a plain checkout —
# the replay is env-limited until a release archive is provided.
_RELEASED_CHARTS_ENV = "TAI_E2E_RELEASED_CHARTS_DIR"

# Old->new replay onto the CURRENT artifacts. Each entry is a previously released
# framework version whose deploy the replay leg renders alongside the working-tree
# artifacts, proving the migrate-before-serve hook gated at BOTH ends of the
# upgrade. The first entry is the first framework release. Empty until a release
# exists to replay from: old->new replay only has meaning from the SECOND framework
# release onward (the first release has no predecessor on the framework to upgrade
# from). Appending a released version string extends the matrix — the leg
# parametrizes over it with no other change.
PREVIOUS_FRAMEWORK_VERSIONS: tuple[str, ...] = ()

# The migrate command both surfaces run, and the compose completion condition
# serve/backend gate on. Named once so every assertion reads the same invariant.
MIGRATE_ARGS = ["db", "migrate"]
GATE_CONDITION = "service_completed_successfully"


def distribution_root() -> Path:
    """The ``tai-distribution`` checkout, or a skip when it is absent.

    ``TAI_E2E_DISTRIBUTION_PATH`` wins; otherwise the sibling ``tai-distribution``
    checkout beside the monorepo. The chart dir must exist — a checkout
    without it is as unusable as a missing one."""
    override = os.environ.get(_DISTRIBUTION_ENV)
    root = Path(override) if override else Path(__file__).resolve().parents[2].parent.parent / "tai-distribution"
    if not (root / "charts" / "tai" / "Chart.yaml").is_file():
        pytest.skip(
            f"tai-distribution checkout not found at {root} (no charts/tai/Chart.yaml); "
            f"clone it beside the monorepo or set {_DISTRIBUTION_ENV}"
        )
    return root


def require_cli(name: str, *, renders: str) -> None:
    """Skip (env-limited) when ``name`` is not on PATH — the leg renders ``renders``
    with it and cannot fabricate the output."""
    if shutil.which(name) is None:
        pytest.skip(f"{name} not on PATH; the upgrade-path leg renders the {renders} with it")


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run ``argv`` to completion, returning stdout; a non-zero exit raises loudly
    with both streams (a broken render is never swallowed into an empty parse)."""
    proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def compose_config(root: Path, *, overlay: bool) -> dict[str, Any]:
    """The fully-resolved compose bundle as ``docker compose config`` renders it
    (JSON), base file alone or base + local build overlay.

    ``POSTGRES_PASSWORD`` is a required (defaulted-less) substitution in the
    bundle, so a throwaway value is supplied to let ``config`` resolve; the leg
    never inspects it. ``TAI_SIBLINGS`` points the overlay's build context at a
    real dir so ``config`` resolves the build stanza without a checkout layout."""
    compose_dir = root / "compose"
    argv = ["docker", "compose", "-f", "docker-compose.yml"]
    if overlay:
        argv += ["-f", "docker-compose.local.yml"]
    argv += ["config", "--format", "json"]
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "e2e-upgrade-path-render-only",
        "TAI_SIBLINGS": str(root.parent),
    }
    return json.loads(_run(argv, cwd=compose_dir, env=env))


def render_schema_init_job(chart_dir: Path, *, quickstart: bool) -> dict[str, Any]:
    """The rendered schema-init Job for one Postgres ownership shape.

    QUICKSTART (``postgresql.enabled=true``) owns its DB; EXTERNAL
    (``postgresql.enabled=false``) requires a pre-existing host + auth secret, set
    here to throwaway coordinates so the template resolves. ``schemaInit.enabled``
    is forced on so the hook renders regardless of which features are toggled —
    the leg is about the hook's command and phase, not the feature gate."""
    sets = ["--set", "schemaInit.enabled=true"]
    if quickstart:
        sets += ["--set", "postgresql.enabled=true"]
    else:
        sets += [
            "--set",
            "postgresql.enabled=false",
            "--set",
            "postgresql.host=e2e-external-pg.invalid",
            "--set",
            "postgresql.auth.existingSecret=e2e-external-db-secret",
        ]
    show = ["--show-only", "templates/schema-init-job.yaml"]
    argv = ["helm", "template", "e2e-upgrade-path", str(chart_dir), *sets, *show]
    rendered = _run(argv, cwd=chart_dir)
    doc = yaml.safe_load(rendered)
    if doc is None:
        raise RuntimeError(f"schema-init-job.yaml rendered empty for chart {chart_dir} (quickstart={quickstart})")
    return doc


def released_chart_dir(base_version: str) -> Path:
    """The chart tree of a previously released framework version, or a skip.

    Resolves ``TAI_E2E_RELEASED_CHARTS_DIR/<version>/charts/tai``; the replay leg
    renders it beside the current chart to prove the migrate hook gated the OLD
    deploy too. Absent in a plain checkout — an env-limited skip, never a fake."""
    base = os.environ.get(_RELEASED_CHARTS_ENV)
    if not base:
        pytest.skip(
            f"no released-chart archive for framework {base_version}; set {_RELEASED_CHARTS_ENV} to a dir "
            "holding <version>/charts/tai trees to run the old->new replay"
        )
    chart = Path(base) / base_version / "charts" / "tai"
    if not (chart / "Chart.yaml").is_file():
        pytest.skip(f"released chart for framework {base_version} not found at {chart}")
    return chart

"""Forge a stack's plugin install by hand, as a marketplace install leaves it.

A spec uses this where the stack must start from a layer a marketplace install could
not build itself: a plugin whose range or dependencies no index serves, or a plugin
that must be importable before the stack ever boots. A stack with the default database
also gets the install-store row the install had written, since a starting server
verifies its plugins directory against those rows.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Json
from tai42_skeleton.marketplace.environments.layout import BuildInput, layer_dir, new_build_id, write_build_record
from tai42_skeleton.marketplace.environments.swap import point_current

from tai42_e2e.topology import StackResources


def lay_wheel_as_current_layer(plugins_dir: Path, wheel: Path, built_from: BuildInput) -> None:
    """Install ``wheel``'s own distribution into a new layer build under ``plugins_dir``,
    record it as built from ``built_from`` and make it the layer's current build.

    ``--no-deps`` because only the plugin's own distribution lands in the layer: its
    dependencies are the image's own, or a range no index serves.
    """
    environment = layer_dir(plugins_dir)
    build_id = new_build_id()
    build = environment.build(build_id)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--prefix", str(build), str(wheel)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install --prefix failed for {wheel.name} (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    write_build_record(build, [built_from], [])
    point_current(environment, build_id)


def record_install_row(resources: StackResources, built_from: BuildInput, plugin_yml: str) -> None:
    """Insert the install-store row a pypi install of ``built_from`` had written, with
    ``plugin_yml`` as its spec, into the stack's own database (the per-stack Postgres clone
    the marketplace store binds to through the ``default`` database)."""
    with (
        psycopg.connect(
            host=resources.pg_host,
            port=resources.pg_port,
            user=resources.pg_user,
            password=resources.pg_password,
            dbname=resources.pg_db,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO marketplace_installs (ref, version, source, spec) VALUES (%s, %s, %s, %s)",
            (built_from.ref, built_from.version, built_from.source, Json(yaml.safe_load(plugin_yml))),
        )
        conn.commit()


def move_install_row_version(resources: StackResources, ref: str, version: str) -> None:
    """Move the install-store row of ``ref`` to ``version`` in the stack's own database, as
    the store reads once that version is the one installed. Raises when no row names ``ref``."""
    with (
        psycopg.connect(
            host=resources.pg_host,
            port=resources.pg_port,
            user=resources.pg_user,
            password=resources.pg_password,
            dbname=resources.pg_db,
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("UPDATE marketplace_installs SET version = %s WHERE ref = %s", (version, ref))
        if cur.rowcount != 1:
            raise RuntimeError(f"the install store holds no row for {ref}; nothing moved to {version}")
        conn.commit()

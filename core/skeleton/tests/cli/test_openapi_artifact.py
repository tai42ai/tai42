"""The committed OpenAPI artifact equals a fresh build in SHAPE.

A route or response-model change that forgets to regenerate ``schemas/openapi.json`` reds
here, naming every path and component that drifted. The artifact's ``info.version`` is the
running package version, written by the release tooling with every bump, so it is a separate
check: a version-only difference is reported as the version line and never as shape drift.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.resources import files

from tai42_skeleton.cli.openapi import build_openapi_spec, openapi_drift, openapi_json


def _committed() -> dict:
    return json.loads(files("tai42_skeleton").joinpath("schemas/openapi.json").read_text(encoding="utf-8"))


def _fresh_in_a_clean_process() -> dict:
    """A fresh build in its own interpreter, so no route a test registered reaches the comparison."""
    code = (
        "import sys\n"
        "from tai42_skeleton.cli.openapi import build_openapi_spec, openapi_json\n"
        "sys.stdout.write(openapi_json(build_openapi_spec()))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_committed_artifact_matches_a_fresh_build() -> None:
    drift = openapi_drift(_fresh_in_a_clean_process(), _committed())
    assert not drift, (
        "the committed OpenAPI artifact is stale — regenerate with "
        "`tai openapi --out core/skeleton/src/tai42_skeleton/schemas/openapi.json`; drift: " + "; ".join(drift)
    )


def test_version_only_difference_is_reported_as_the_version_line() -> None:
    fresh = build_openapi_spec()
    committed = json.loads(openapi_json(fresh))
    committed["info"]["version"] = "0.0.0-not-the-running-version"
    drift = openapi_drift(fresh, committed)
    assert drift == [
        "info.version '0.0.0-not-the-running-version' in the committed artifact does not match the running "
        f"package version {fresh['info']['version']!r}"
    ]


def test_a_reshaped_component_is_named_and_no_version_line() -> None:
    fresh = build_openapi_spec()
    committed = json.loads(openapi_json(fresh))
    committed["components"]["schemas"]["Error"]["properties"]["_added_by_test"] = {"type": "string"}
    drift = openapi_drift(fresh, committed)
    assert drift == ["component 'Error' changed shape"]


def test_paths_and_the_envelope_are_reported_by_their_own_lines() -> None:
    fresh = build_openapi_spec()
    committed = json.loads(openapi_json(fresh))
    removed_path = sorted(committed["paths"])[0]
    del committed["paths"][removed_path]
    committed["paths"]["/api/_added_by_test"] = {}
    del committed["components"]["schemas"]["ReloadingError"]
    committed["components"]["schemas"]["_AddedByTest"] = {"type": "object"}
    committed["info"]["title"] = "another title"
    drift = openapi_drift(fresh, committed)
    assert drift == [
        "path '/api/_added_by_test' was removed",
        f"path {removed_path!r} is new",
        "component 'ReloadingError' is new",
        "component '_AddedByTest' was removed",
        "the spec envelope changed",
    ]


def test_the_artifact_is_canonical_json() -> None:
    spec = build_openapi_spec()
    text = openapi_json(spec)
    assert text == json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_the_spec_states_its_scope() -> None:
    assert build_openapi_spec()["info"]["description"].endswith(
        "The platform's core routes. Plugin routes are published by each plugin."
    )

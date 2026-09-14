"""Preset operations — author and read versioned presets over the live engine.

A preset is a base tool + baked ``fixed_kwargs`` + extension combos, persisted as a
versioned document and registered as a live named tool. These operations are the
single source of truth for the preset surface: the HTTP routes in
``routers/presets.py`` are thin adapters over them, and the MCP projection binds
each as a tool (so extensions wrap it and other presets can bake over it).

Every mutating door validates a body CAN bind BEFORE any store write (a bad edit is
a loud 400, never a committed version that can never bind), persists THEN registers,
compensates a residual register failure by re-pointing the store so store + live
never diverge, and fans the rebind/removal out on the worker bus — embedding that
per-worker fleet report in the mutation response (the ``fanout`` field, the same shape
the template writers return) so a deployer has the read-your-writes barrier signal the
platform already computes, not merely a log line. The concrete app
singleton (``from tai42_skeleton.app import instance``) is reached for the store view,
the register/reload engine, the generic versioned store (the HARD delete the view
does not expose), and ``emit_list_changed``. Success values are returned bare — the
route adapter wraps them in ``{"data": ...}`` at the HTTP edge.

The ``@operation`` doors, request models, body readers and the private helpers external
modules and tests reach for are re-exported here, so ``tai42_skeleton.operations.presets``
stays the one public path. ``_agent_tool_names``, ``_agent_authoring_error`` and
``advisory_name_lock`` are bound as package attributes and read through this package object
at call time by the door submodules, so a test's ``monkeypatch.setattr`` on either the
package alias holds across the split (and a hot reload).
"""

from __future__ import annotations

from tai42_skeleton.db import advisory_name_lock as advisory_name_lock
from tai42_skeleton.operations import BadRequestError as BadRequestError
from tai42_skeleton.operations import ConflictError as ConflictError
from tai42_skeleton.operations import NotFoundError as NotFoundError
from tai42_skeleton.operations import NotSupportedError as NotSupportedError
from tai42_skeleton.operations._authority import resolve_caller as resolve_caller
from tai42_skeleton.operations.presets.authoring import (
    _agent_authoring_error as _agent_authoring_error,
)
from tai42_skeleton.operations.presets.authoring import (
    _enforce_registration_tier as _enforce_registration_tier,
)
from tai42_skeleton.operations.presets.authoring import (
    _input_schema_authoring_error as _input_schema_authoring_error,
)
from tai42_skeleton.operations.presets.create import _apply_one_seed as _apply_one_seed
from tai42_skeleton.operations.presets.create import _create_preset_core as _create_preset_core
from tai42_skeleton.operations.presets.create import apply_preset_seeds as apply_preset_seeds
from tai42_skeleton.operations.presets.create import create_preset as create_preset
from tai42_skeleton.operations.presets.models import PresetCreate as PresetCreate
from tai42_skeleton.operations.presets.models import PresetRename as PresetRename
from tai42_skeleton.operations.presets.models import PresetRollback as PresetRollback
from tai42_skeleton.operations.presets.models import PresetValidate as PresetValidate
from tai42_skeleton.operations.presets.models import PresetVersionSave as PresetVersionSave
from tai42_skeleton.operations.presets.models import PresetVersionTags as PresetVersionTags
from tai42_skeleton.operations.presets.read import get_preset as get_preset
from tai42_skeleton.operations.presets.read import get_version as get_version
from tai42_skeleton.operations.presets.read import list_presets as list_presets
from tai42_skeleton.operations.presets.read import list_versions as list_versions
from tai42_skeleton.operations.presets.read import preset_referees as preset_referees
from tai42_skeleton.operations.presets.readers import read_combos as read_combos
from tai42_skeleton.operations.presets.readers import read_create_extensions as read_create_extensions
from tai42_skeleton.operations.presets.readers import read_edit_extensions as read_edit_extensions
from tai42_skeleton.operations.presets.readers import read_element as read_element
from tai42_skeleton.operations.presets.readers import read_input_schema as read_input_schema
from tai42_skeleton.operations.presets.readers import read_output_schema as read_output_schema
from tai42_skeleton.operations.presets.readers import read_state_binding as read_state_binding
from tai42_skeleton.operations.presets.references import _agent_tool_names as _agent_tool_names
from tai42_skeleton.operations.presets.references import _referenced_tool_names as _referenced_tool_names
from tai42_skeleton.operations.presets.rename import delete_preset as delete_preset
from tai42_skeleton.operations.presets.rename import rename_preset as rename_preset
from tai42_skeleton.operations.presets.validate import validate_preset as validate_preset
from tai42_skeleton.operations.presets.versions import _save_version_core as _save_version_core
from tai42_skeleton.operations.presets.versions import rollback_preset as rollback_preset
from tai42_skeleton.operations.presets.versions import save_version as save_version
from tai42_skeleton.operations.presets.versions import set_preset_version_tags as set_preset_version_tags
from tai42_skeleton.operations.presets.views import _create_response as _create_response
from tai42_skeleton.operations.presets.views import _save_version_response as _save_version_response

__all__ = [
    "BadRequestError",
    "ConflictError",
    "NotFoundError",
    "NotSupportedError",
    "PresetCreate",
    "PresetRename",
    "PresetRollback",
    "PresetValidate",
    "PresetVersionSave",
    "PresetVersionTags",
    "advisory_name_lock",
    "apply_preset_seeds",
    "create_preset",
    "delete_preset",
    "get_preset",
    "get_version",
    "list_presets",
    "list_versions",
    "preset_referees",
    "read_combos",
    "read_create_extensions",
    "read_edit_extensions",
    "read_element",
    "read_input_schema",
    "read_output_schema",
    "read_state_binding",
    "rename_preset",
    "resolve_caller",
    "rollback_preset",
    "save_version",
    "set_preset_version_tags",
    "validate_preset",
]

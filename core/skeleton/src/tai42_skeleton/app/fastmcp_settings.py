"""fastmcp's process-wide settings, read under the env the boot env bridge applied.

fastmcp reads its ``FASTMCP_*`` settings once, when it is imported: from ``os.environ`` and from
the env file ``FASTMCP_ENV_FILE`` names (``.env`` in the working directory by default). In the
``file`` config mode that file can be the stored env, which the app applies itself, filtered by
ownership, and which no settings source reads directly; and the import comes before the bridge
applies the stored env. :func:`apply_fastmcp_settings` reads fastmcp's settings again once the
bridge has applied it — from ``os.environ`` and from fastmcp's env file unless that file is the
stored env (:func:`~tai42_kit.settings.env_file_in_effect`), its nested docket settings under
their own names as well — and assigns every value onto the
``fastmcp.settings`` object in place, through fastmcp's ``set_setting``: every fastmcp module
reads that one object. :func:`apply_fastmcp_logging` re-runs the logging setup fastmcp runs at
import, on the values read again. That object is process-wide, so only a CLI entrypoint, which
owns its process, registers both (:func:`~tai42_skeleton.app.instance.register_cli_process_state`);
an embedded app leaves the host's fastmcp settings as the host set them.

fastmcp's settings read the applied env, so they are declared an env reader at import
(:func:`~tai42_kit.settings.declare_env_reader`): a ``FASTMCP_*`` setting the deployment sets is
the deployment's under every name fastmcp reads for it, its nested docket settings' names
included.
"""

from __future__ import annotations

from typing import Any

import fastmcp
from fastmcp.settings import Settings
from fastmcp.utilities.logging import configure_logging
from pydantic import create_model
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings
from tai42_kit.settings import declare_env_reader, env_file_in_effect

declare_env_reader(Settings)


def apply_fastmcp_settings() -> None:
    """Read fastmcp's settings under the current env and assign every value onto ``fastmcp.settings``.

    The env file read is the one fastmcp declares, unless it resolves to the stored env. A value
    the read does not validate raises, through fastmcp's own validation; so does an env file
    declaration other than one path.
    """
    declared = Settings.model_config.get("env_file")
    if declared is not None and not isinstance(declared, str):
        raise TypeError(f"fastmcp declares its env file as {declared!r}; a single path string is read here")
    current = fastmcp.settings
    fresh = _read_settings(env_file_in_effect(declared))
    for name in Settings.model_fields:
        current.set_setting(name, getattr(fresh, name))


def _read_settings(env_file: str | None) -> Settings:
    """Read fastmcp's settings as an import under the current env reads them.

    A nested settings group (the docket settings) defaults to an instance fastmcp built when it was
    imported, under the env of that moment, and fastmcp's nested names update that default in
    part. The read here gives each such group a default read now, so the group's own names and
    fastmcp's nested names both read the current env.
    """
    fresh_defaults: dict[str, Any] = {
        name: (field.annotation, FieldInfo.merge_field_infos(field, default=type(field.default)()))
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, BaseSettings)
    }
    current_read = create_model(Settings.__name__, __base__=Settings, __module__=Settings.__module__, **fresh_defaults)
    # ``_env_file`` is pydantic-settings' init argument naming the env file one construction
    # reads; the field-derived signature the type checker sees leaves it out.
    return current_read(_env_file=env_file)  # pyright: ignore[reportCallIssue]


def apply_fastmcp_logging() -> None:
    """Configure fastmcp's logger from ``fastmcp.settings``, as fastmcp does when it is imported.

    Only an entrypoint that owns its process's logging calls this; an embedded app leaves the
    host's fastmcp logger untouched.
    """
    settings = fastmcp.settings
    if settings.log_enabled:
        configure_logging(level=settings.log_level, enable_rich_tracebacks=settings.enable_rich_tracebacks)

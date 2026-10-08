"""The boot, reload and env-write audit of owned settings prefixes.

A settings group that declares its ``env_prefix`` owned (``TaiBaseSettings.env_prefix_owned``)
refuses every env name under that prefix that no registered group accepts. This module makes
sure the owning groups are registered before an audit runs, audits the env every boot and reload
applies, and audits the env inputs a process reads at boot.
"""

import importlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from dotenv import dotenv_values
from tai42_kit.settings import refuse_unknown_owned_env

# The modules whose settings groups own an env prefix. Importing one registers its groups.
OWNED_SETTINGS_MODULES: tuple[str, ...] = (
    "tai42_skeleton.access_control.settings",
    "tai42_skeleton.interactions.settings",
    "tai42_skeleton.channels.settings",
)

# The ``.env`` file ``TaiBaseSettings`` reads (its ``env_file``), relative to the working directory.
_DOTENV_FILE: Final = ".env"


def register_owned_settings_groups() -> None:
    """Import every module of ``OWNED_SETTINGS_MODULES`` so its owning groups are registered."""
    for module in OWNED_SETTINGS_MODULES:
        importlib.import_module(module)


def refuse_unknown_owned_env_write(keys: Iterable[str]) -> None:
    """Refuse an env write that sets a name under an owned prefix no registered group accepts.

    ``keys`` are the names the write SETS; a deletion is never audited, since deleting an
    unknown name is its remedy.
    """
    register_owned_settings_groups()
    refuse_unknown_owned_env(keys, source="env write")


def refuse_unknown_owned_applied_env(keys: Iterable[str]) -> None:
    """Refuse the env applied to the process when it names an unknown setting under an owned prefix.

    ``keys`` are the names applied from the stored env at boot, or proposed by a reload (the env
    it persists). Audited without the Kubernetes service-link exception: nothing injects service
    links into the stored env.
    """
    register_owned_settings_groups()
    refuse_unknown_owned_env(keys, source="stored env")


def require_known_owned_settings() -> None:
    """Refuse boot when the process's own env inputs name an unknown setting under an owned prefix.

    Audits the ``.env`` file in the working directory when it exists, then the process
    environment, each as its own source so the message names where the key was read. Only the
    process environment gets the Kubernetes service-link exception: nothing injects service
    links into the file.
    """
    register_owned_settings_groups()
    if Path(_DOTENV_FILE).is_file():
        refuse_unknown_owned_env(dotenv_values(_DOTENV_FILE), source="boot (.env file)")
    refuse_unknown_owned_env(os.environ, source="boot (process environment)", service_link_env=os.environ)

"""The boot ``.env`` load: a local ``.env`` into ``os.environ``, never the ``file`` provider's stored env.

In ``file`` config mode every process entry point (the ``tai`` root callback and each server launcher)
loads a local ``.env`` before anything else reads the environment. The ``file`` provider's stored env
(``.env`` in its config dir) is not such a file: the server applies its keys itself, after it has
captured the environment it was started with as the deployment env, and a stored key loaded here
would be counted as set by the deployment. ``python-dotenv``'s search can land on that file (it
searches from the working directory under a trace function, interactively, or when frozen, and from
the caller's folder upward otherwise), so the load skips it and logs a warning. The stored env is
located from the deployment's own statement only (``TAI_CONFIG_DIR_PATH`` in the environment, else
``/app``): the config dir is deployment identity, which the stored env never carries, so a found
``.env`` that names its own folder as the config dir is refused.
"""

from __future__ import annotations

import logging
import os

from dotenv import dotenv_values, load_dotenv
from tai42_contract.config.manager import CONFIG_DIR_ENV, file_config_dir, file_stored_env_path

logger = logging.getLogger(__name__)


def _is_file(dotenv_path: str, candidate: str) -> bool:
    return os.path.exists(candidate) and os.path.samefile(dotenv_path, candidate)


def load_boot_dotenv(dotenv_path: str) -> None:
    """Load ``dotenv_path`` into ``os.environ``, leaving set names as they are, unless it is the stored env.

    ``dotenv_path`` is the file ``find_dotenv()`` located for the calling entry point, empty when it
    found none. The stored env is the ``.env`` of the config dir the deployment names:
    ``TAI_CONFIG_DIR_PATH`` in the environment, else ``/app``. When ``dotenv_path`` is that file,
    nothing is loaded and a warning names it. A found ``.env`` that names its own folder as the config
    dir from inside is refused with a ``RuntimeError``, and nothing is loaded.
    """
    if not dotenv_path:
        return
    if _is_file(dotenv_path, file_stored_env_path(os.environ)):
        logger.warning(
            "Not loading %s into the environment at boot: it is the stored env of the config dir %s, "
            "which the server applies itself",
            dotenv_path,
            file_config_dir(os.environ),
        )
        return
    file_values = {key: value for key, value in dotenv_values(dotenv_path).items() if value is not None}
    if _is_file(dotenv_path, file_stored_env_path({**file_values, **os.environ})):
        raise RuntimeError(
            f"Refusing to start: {dotenv_path} names its own folder as the config dir "
            f"({CONFIG_DIR_ENV}={file_values[CONFIG_DIR_ENV]!r}), which makes it the stored env; the stored env "
            f"never carries the config dir. Set {CONFIG_DIR_ENV} in the environment and remove it from the file."
        )
    load_dotenv(dotenv_path)

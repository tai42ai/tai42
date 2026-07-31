import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_kit.logging.settings import LoggingSettings


def setup_logging(settings: "LoggingSettings") -> None:
    """Configure the ROOT logger for an application.

    Call this from application startup only — never on library import, since it
    reconfigures the process-global root logger. ``force=True`` replaces the
    existing root handlers, so a repeat call (e.g. after a settings reload)
    applies the new level instead of being silently ignored.
    """
    mapping = logging.getLevelNamesMapping()
    level_name = settings.log_level.upper()
    if level_name not in mapping:
        raise ValueError(f"Invalid log level: {settings.log_level!r}. Must be one of {sorted(mapping)}")
    logging.basicConfig(
        level=mapping[level_name],
        format="[%(asctime)s] %(levelname)-8s %(name)s %(message)-30s",
        datefmt="%m/%d/%y %H:%M:%S",
        force=True,
    )

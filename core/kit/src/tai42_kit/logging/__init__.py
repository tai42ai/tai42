"""Logging settings + setup, co-located with the logging util.

Side-effect-free on import: the consumer calls ``setup_logging(...)`` explicitly
(e.g. at app startup) — importing this package configures nothing on its own.
"""

from tai42_kit.logging.logger import setup_logging
from tai42_kit.logging.settings import LoggingSettings, logging_settings

__all__ = ["LoggingSettings", "logging_settings", "setup_logging"]

"""A backend loop process's liveness file, and the probe that reads it.

A loop process reports each pass of its loop, with the seconds within which its next pass is
due, into one file (:class:`LivenessFile`); an exec probe (``tai42-backend-probe``,
:mod:`tai42_kit.liveness.probe`) reports it alive while that promise is not overdue by more than
a grace (:func:`check_liveness`). The settings group :class:`BackendLivenessSettings`
(``TAI_BACKEND_LIVENESS_``) names the path, the refresh interval and the grace.

This subpackage imports only :mod:`tai42_kit.settings` and the standard library, so the probe
pays for nothing else.
"""

from tai42_kit.liveness.file import LivenessCheck, LivenessFile, check_liveness
from tai42_kit.liveness.settings import BackendLivenessSettings, backend_liveness_settings

__all__ = [
    "BackendLivenessSettings",
    "LivenessCheck",
    "LivenessFile",
    "backend_liveness_settings",
    "check_liveness",
]

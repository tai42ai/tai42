"""Bind the process app singleton before this directory's test modules import.

Router modules resolve routes against the ``tai42_app`` handle at import time, exactly
as external plugins do; the runtime imports them only after ``start()`` binds the handle.
The facet-door test modules here import the routers at collection to compare the
in-process and HTTP doors, so mirror that order: bind the singleton first.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance

tai42_app.bind(instance.build_app())

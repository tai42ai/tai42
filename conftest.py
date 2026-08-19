"""Repo-root conftest: whole-repo session setup loaded before any collection.

Only in effect for a whole-repo run (rootdir here); scoped member runs anchor
their own pyproject as rootdir and never load this file.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# ``prometheus_client`` freezes its value backend (multiprocess mmap vs in-process
# mutex) the first time it is imported, choosing mmap only when
# ``PROMETHEUS_MULTIPROC_DIR`` is already set. The skeleton writer paths assert the
# mmap backend. This rootdir conftest is imported before any test module, so set
# the var to a fresh per-session dir here to freeze mmap session-wide. A
# caller-provided value is left untouched.
if "PROMETHEUS_MULTIPROC_DIR" not in os.environ:
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix="tai42_prometheus_")
    atexit.register(shutil.rmtree, os.environ["PROMETHEUS_MULTIPROC_DIR"], ignore_errors=True)

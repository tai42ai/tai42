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
# mmap backend. This rootdir conftest is imported before any test module, so point
# the var at a fresh dir here to freeze mmap session-wide.
#
# Tests write real counters into that dir and the monitoring health watch sums
# every per-pid file in it and keeps its alert state there, so each test process
# owns its own dir: the dir name carries the owning pid, and a value inherited
# from another process (the pytest-xdist controller, a shell export, a parent
# test run) is replaced, never shared. The skeleton suite's conftest applies the
# same rule, so within one process the first conftest's dir stands.
if not os.path.basename(os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")).startswith(
    _own_prefix := f"tai42_prometheus_{os.getpid()}_"
):
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix=_own_prefix)
    atexit.register(shutil.rmtree, os.environ["PROMETHEUS_MULTIPROC_DIR"], ignore_errors=True)

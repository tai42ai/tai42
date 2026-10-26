"""Harness self-test: a process ``spawn_expect_refusal`` expected to refuse but that served.

The process is killed, the failure names its argv and carries the stderr it wrote, and the
pipes the spawn opened are closed, so no unclosed-file warning reaches a later test.
"""

from __future__ import annotations

import gc
import sys
import warnings
from pathlib import Path

import pytest

from tai42_e2e.binaries import spawn_expect_refusal

pytestmark = pytest.mark.needs("no-stack")

# The line it writes is joined at run time, so only its stderr carries the joined text, never its argv.
_SERVES = "import sys, time; sys.stderr.write('-'.join(['still', 'up'])); sys.stderr.flush(); time.sleep(60)"


def test_a_process_that_serves_fails_with_its_stderr_and_leaves_no_pipe_open(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", _SERVES]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with pytest.raises(RuntimeError, match="it served") as exc:
            spawn_expect_refusal(argv, {}, tmp_path, timeout=2.0)
        message = str(exc.value)
        # The failure's traceback holds the spawn's frame; once it is dropped, the spawn's
        # objects are collected here.
        del exc
        gc.collect()

    assert "still-up" in message
    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)] == []

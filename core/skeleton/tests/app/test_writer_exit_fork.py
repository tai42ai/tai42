"""A forked job child settles its writer-exit scope while its parent's thread imports the exit's module.

A fork copies the interpreter's per-module import locks as they stand at the fork instant, and
``importlib`` resets none of them in the child: a module another thread of the parent was
importing at that instant stays locked in the child by a thread that does not exist there, and
the child's first import of it never returns. A backend worker forks its job children while its
serving loop runs, so the exit a job's change settles through must already be loaded wherever
the scope is: settling imports nothing.

The check runs in a fresh interpreter, where the exit's module is loaded only as the scope's
module loads it, and forks there while a second thread holds that module's import open.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")

_CHILD_SETTLES_WITHIN = 10.0

_DRIVER = textwrap.dedent(
    """
    import asyncio
    import importlib
    import importlib.machinery
    import os
    import sys
    import threading
    import time

    from tai42_skeleton.app.writer_exit import writer_exit_scope

    EXIT_MODULE = "tai42_skeleton.app.graceful_exit"
    entered = threading.Event()
    release = threading.Event()


    class HeldImport:
        def find_spec(self, name, path=None, target=None):
            if name != EXIT_MODULE:
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            run_body = spec.loader.exec_module

            def exec_module(module):
                entered.set()
                release.wait()
                run_body(module)

            spec.loader.exec_module = exec_module
            return spec


    sys.meta_path.insert(0, HeldImport())
    importer = threading.Thread(target=importlib.import_module, args=(EXIT_MODULE,))
    importer.start()
    while importer.is_alive() and not entered.is_set():
        time.sleep(0.01)


    async def settle():
        with writer_exit_scope() as exits:
            await exits.settle()


    pid = os.fork()
    if pid == 0:
        asyncio.run(settle())
        os._exit(0)
    deadline = time.monotonic() + float(sys.argv[1])
    status = None
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        status = None
    release.set()
    importer.join()
    if status is None:
        print("the forked child never finished settling its writer-exit scope")
        sys.exit(1)
    sys.exit(os.waitstatus_to_exitcode(status))
    """
)


def test_a_child_forked_mid_import_of_the_exit_module_settles_its_scope() -> None:
    driver = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(_CHILD_SETTLES_WITHIN)],
        capture_output=True,
        text=True,
        timeout=_CHILD_SETTLES_WITHIN * 3,
        check=False,
    )

    assert driver.returncode == 0, driver.stdout + driver.stderr

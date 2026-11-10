"""A job module that imports in the caller and raises ``RuntimeError`` when a worker imports it."""

import multiprocessing

if multiprocessing.parent_process() is not None:
    raise RuntimeError("no app bound")


def job() -> int:
    return 1

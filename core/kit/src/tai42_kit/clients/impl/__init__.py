"""Concrete pooled clients. Each imports its driver at module top, so import the
submodule only with the matching extra installed (``tai42-kit[redis]`` / ``[curl]``
/ ``[postgres]``)."""

"""The door-side classification of a stored definition's write-service errors.

Each error class maps by EXACT type: a refused definition is a 4xx, and every other failure (an
unbound store, a store fault, any class not named here) propagates out of the operation as a 500,
never mis-reported as a client error.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from tai42_contract.states.errors import AttachConflictError, StateNotFoundError, StatesError

from tai42_skeleton.operations.errors import BadRequestError, ConflictError, NotFoundError
from tai42_skeleton.tools.state_binding import is_binding_refusal


@contextmanager
def definition_door(definition: str) -> Iterator[None]:
    """Answer a refused definition as a 4xx and let every other failure surface as a 500.

    A binding refusal is a 404 when it names something absent (a state, a template, a program), a
    409 for an occupied attach path, otherwise a 400; a non-store ``ValueError`` (a jq that does
    not compile, a model that does not validate, a write service's own refusal) is a 400. The
    message is ``invalid <definition>: <the refusal's own text>``.
    """
    try:
        yield
    except StatesError as exc:
        if not is_binding_refusal(exc):
            raise
        status: type[BadRequestError | ConflictError | NotFoundError]
        if type(exc) is StateNotFoundError:
            status = NotFoundError
        elif type(exc) is AttachConflictError:
            status = ConflictError
        else:
            status = BadRequestError
        raise status(f"invalid {definition}: {exc}", extra=exc.extra) from exc
    except ValueError as exc:
        raise BadRequestError(f"invalid {definition}: {exc}") from exc


__all__ = ["definition_door"]

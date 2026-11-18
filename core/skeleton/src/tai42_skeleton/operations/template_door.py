"""The operations door over the template layer: its typed refusals answered with an operation error.

The template layer raises its own errors and knows no status; every operation that enters its
load or delete path runs the call inside :func:`template_door`, which answers each refusal with
the operation error the adapter maps to a status. A held object's refusal is rendered in the acting caller's view.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tai42_skeleton.operations.errors import NotSupportedError, OperationFailedError
from tai42_skeleton.operations.holders import holders_refusal
from tai42_skeleton.template.errors import FilesNotConfiguredError, HolderUnreadableError, StoredObjectHeldError


@asynccontextmanager
async def template_door() -> AsyncIterator[None]:
    """Translate the template layer's typed refusals into the operation error the adapter maps to a status.

    The one seam every operation runs a template-layer load or delete through: a held object is the
    ``409`` naming the holders the caller may read, an unreadable holder the ``500`` with the
    same text, a missing ``files`` extra the ``501`` carrying its code. Any other error passes
    through as it is.
    """
    try:
        yield
    except StoredObjectHeldError as exc:
        raise await holders_refusal(exc.head, exc.holders, "remove those references first") from exc
    except HolderUnreadableError as exc:
        raise OperationFailedError(str(exc)) from exc
    except FilesNotConfiguredError as exc:
        raise NotSupportedError(str(exc), extra={"code": exc.code}) from exc


__all__ = ["template_door"]

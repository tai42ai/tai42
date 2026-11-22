"""The error types of the store and content-load doors.

A refusal the operations door answers with a status carries the contract's ``ErrorKind``
(``__tai_error_kind__``), so a caller with no HTTP door classifies it the same way.
"""

from collections.abc import Sequence

from tai42_contract.errors import ErrorKind
from tai42_contract.tools import ReferenceHolder

# The machine-readable code every content load answers when the skeleton's ``files``
# extra is not installed; the message is the install line.
FILES_NOT_CONFIGURED_CODE = "files-not-configured"


class StoredObjectHeldError(Exception):
    """A delete refused because stored objects still name what it would remove.

    ``head`` says what cannot be deleted; ``holders`` are every place that names it, in the
    referees' order. Nothing was deleted.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT

    def __init__(self, head: str, holders: Sequence[ReferenceHolder]) -> None:
        """Refuse the delete ``head`` names over ``holders``."""
        self.head = head
        self.holders = list(holders)
        super().__init__("\n".join([head, *(holder.summary for holder in self.holders)]))


class HolderUnreadableError(Exception):
    """A delete refused because a holder of what it would remove could not be read.

    The text carries only the fault's type name, never its message (a settings validation
    error quotes the value); the fault is the cause. Nothing was deleted.
    """

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, head: str, fault_type: str) -> None:
        """Refuse the delete ``head`` names because a holder raised a ``fault_type``."""
        self.head = head
        super().__init__(f"{head}: a holder could not be read ({fault_type})")


class PartialDirectoryDeleteError(Exception):
    """A directory delete failed once the provider had begun deleting.

    Some, none or all of the objects under ``path`` may be gone, and which is unknown. The
    render state under the directory is evicted; the provider's failure is the cause.
    """

    def __init__(self, path: str, fault: BaseException) -> None:
        """Report the failed delete of directory ``path`` the provider raised ``fault`` for."""
        self.path = path
        super().__init__(f"directory '{path}' delete failed after deletion began: {fault}")


class FilesNotConfiguredError(Exception):
    """A content load refused because the skeleton's ``files`` extra is not installed.

    The message is the install line.
    """

    __tai_error_kind__ = ErrorKind.UNAVAILABLE
    code = FILES_NOT_CONFIGURED_CODE


class ContentNotLoadableError(ValueError):
    """Loaded bytes cannot be turned into text or media by :meth:`ResourceManager.load_file`.

    Raised for bytes of a type no loader handles, bytes over the
    ``FILE_LOADING_MAX_BYTES`` cap, bytes detected as text that are not UTF-8 (a
    strict decode, never a lossy substitution), and an extracting handler's failure on
    the stored bytes (an encrypted or truncated PDF, a ragged CSV; the parser's error is
    the cause). The content is the problem, so it is
    a client error on every door that loads content; genuine storage/transport
    failures are NOT this type and keep propagating.
    """

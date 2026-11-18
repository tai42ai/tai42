"""The template layer's error types and the wording of a refused render.

A refusal the operations door answers with a status carries the contract's ``ErrorKind``
(``__tai_error_kind__``), so a caller with no HTTP door classifies it the same way.
"""

from collections.abc import Sequence

from jinja2 import TemplateError
from tai42_contract.errors import ErrorKind
from tai42_contract.template import TemplatedText
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


class TemplateNotFoundError(Exception):
    """A requested stored template does not exist.

    Raised by the read paths when a ``template_id`` resolves to no stored
    content, so an HTTP surface can map a missing template to 404 instead of
    leaking storage's ``FileNotFoundError`` as a 500. Genuine storage/transport
    failures are NOT this type and keep propagating.
    """


class TemplateNotTextError(TemplateError):
    """A requested stored object exists but is not UTF-8 text, so it is not a template.

    Templates share the storage provider with every other stored object (images,
    documents), so a ``template_id`` can name a binary object. Every by-id read of
    template text raises this instead of the provider's ``UnicodeDecodeError``; as a
    ``jinja2.TemplateError`` it is a client error on every surface that maps template
    errors. Genuine storage/transport failures are NOT this type and keep propagating.
    """


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


class TemplateLocaleNotFoundError(Exception):
    """A stored template has no variant for the resolved locale chain and no default variant.

    Raised by :meth:`ResourceManager.render_by_id` when a locale-scoped render walks the
    chain (``he-IL`` -> ``he`` -> the bare default id) and finds NONE of them stored, so
    the render refuses loudly naming the template and locale instead of silently rendering
    the wrong language.
    """


#: Every way :meth:`ResourceManager.render_templated_text` fails because of the template
#: itself: a stored id that is absent, absent for the locale chain, or not text, and a body
#: (stored or inline) the engine cannot parse or render. A door that renders a templated
#: text and refuses a bad one catches this tuple and words the refusal with
#: :func:`template_render_refusal`; genuine storage failures are not in it.
TEMPLATE_RENDER_ERRORS: tuple[type[Exception], ...] = (
    TemplateNotFoundError,
    TemplateLocaleNotFoundError,
    TemplateError,
)

#: The subset of :data:`TEMPLATE_RENDER_ERRORS` raised when a stored id yields no template text.
_TEMPLATE_FETCH_ERRORS: tuple[type[Exception], ...] = (
    TemplateNotFoundError,
    TemplateLocaleNotFoundError,
    TemplateNotTextError,
)


def template_render_refusal(text: TemplatedText, exc: Exception) -> str:
    """The predicate of a refusal for ``text`` whose render raised ``exc`` (one of :data:`TEMPLATE_RENDER_ERRORS`).

    A door prefixes it with what it rendered (a field, a slot, a program). A stored id that
    yields no text "could not be fetched"; a stored body the engine cannot render "could not
    be rendered"; an inline body "is not a renderable template". ``exc``'s own message closes
    the text.
    """
    if text.id is None:
        return f"is not a renderable template: {exc}"
    if isinstance(exc, _TEMPLATE_FETCH_ERRORS):
        return f"references stored id {text.id!r}, which could not be fetched: {exc}"
    return f"references stored id {text.id!r}, which could not be rendered: {exc}"

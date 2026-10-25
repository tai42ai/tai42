"""The template layer's error types and the wording of a refused render."""

from jinja2 import TemplateError
from tai42_contract.template import TemplatedText

# The machine-readable code every content load answers when the skeleton's ``files``
# extra is not installed; the message is the install line.
FILES_NOT_CONFIGURED_CODE = "files-not-configured"


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

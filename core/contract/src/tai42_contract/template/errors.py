"""The error set of the templated-text render seam.

``resource_manager.render_templated_text(text, locale)`` raises a :class:`TemplateRenderError`
when, and only when, the templated text as authored cannot be rendered; every other failure
(the store, the network, an unconfigured manager, a fault in the renderer's own code)
propagates as itself. A door that refuses an author's text catches :class:`TemplateRenderError`
and nothing broader. Every member carries the base's ``BAD_INPUT`` kind: the family means one
thing, the author's text is at fault.
"""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class TemplateRenderError(Exception):
    """The templated text as authored cannot be rendered.

    ``str(exc)`` is the author-facing text: it names the stored id (or says the text is
    inline) and the cause. A door prefixes the field it rendered.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class TemplateNotFoundError(TemplateRenderError):
    """A stored id the text names is not stored: its own, one its body includes, or its variant for the locale chain."""


class TemplateNotTextError(TemplateRenderError):
    """The stored item at the id is not UTF-8 text."""


class TemplateBodyError(TemplateRenderError):
    """The body does not parse or render.

    A syntax error, an undefined variable, a construct the sandbox refuses, a bad filter argument.
    """


__all__ = [
    "TemplateBodyError",
    "TemplateNotFoundError",
    "TemplateNotTextError",
    "TemplateRenderError",
]

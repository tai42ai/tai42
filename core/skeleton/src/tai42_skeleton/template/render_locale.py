"""The subject's locale in a render: the variant fallback chain, the stamped context key, ``list_format``.

A stored template may carry per-locale variants at ``"{template_id}@{subtags}"``; a render for a
locale resolves the most specific stored variant down the fallback chain. The resolved locale is
stamped onto the render context under a reserved key, which the ``list_format`` filter reads.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from babel.core import Locale, UnknownLocaleError
from babel.lists import format_list
from jinja2 import Template as JinjaTemplate
from jinja2 import pass_context

from tai42_skeleton.template.errors import TemplateLocaleNotFoundError, TemplateNotFoundError

#: The reserved render-context key the rendering layer stamps the subject's locale onto,
#: read by :func:`list_format` — never a template author's variable (a collision raises).
LOCALE_CONTEXT_KEY = "_tai_locale"


@pass_context
def list_format(context: Any, value: Any, style: str = "standard") -> str:
    """Format a sequence with the CLDR list patterns of the render's subject locale.

    A template writes ``{{ names | list_format }}`` and the separator + final connective
    come from the locale the rendering layer resolved (``_tai_locale`` in the render
    context) — the template never selects a language. ``style`` picks a CLDR list style
    (``standard`` and/or babel's ``standard-short``, ``or``, ``unit``). A render that
    resolved NO locale, or a locale babel has no CLDR data for, raises loudly rather than
    guessing a separator.
    """
    locale = context.get(LOCALE_CONTEXT_KEY)
    if locale is None:
        raise ValueError(
            "list_format needs the subject's locale, but this render resolved none; the "
            "channel or operator must supply a locale for the platform to format a list"
        )
    items = [str(item) for item in value]
    try:
        # The subject's locale is a BCP 47 tag (``-`` separator); babel keys CLDR data by
        # its own ``_`` identifier, so parse the tag rather than hand it the raw string.
        parsed = Locale.parse(locale, sep="-")
    except (UnknownLocaleError, ValueError) as exc:
        raise ValueError(f"list_format has no CLDR list patterns for locale {locale!r}") from exc
    return format_list(items, style=cast(Any, style), locale=parsed)


def locale_variant_ids(template_id: str, locale: str) -> list[str]:
    """The ordered fallback chain of variant ids for ``template_id`` under ``locale``.

    The most-specific subtag form first, each shorter form next, then the bare
    ``template_id`` (the declared default variant). A variant lives at
    ``"{template_id}@{subtags}"`` — e.g. ``he-IL`` yields ``id@he-IL``, ``id@he``,
    ``id``. The locale is already canonical, so the chain is built from one spelling.
    """
    subs = locale.split("-")
    ids = [f"{template_id}@{'-'.join(subs[:i])}" for i in range(len(subs), 0, -1)]
    ids.append(template_id)
    return ids


async def compiled_for_locale(
    template_id: str, locale: str | None, compiled: Callable[[str], Awaitable[JinjaTemplate]]
) -> JinjaTemplate:
    """Resolve ``template_id`` to a compiled template for ``locale`` through ``compiled``.

    ``compiled`` resolves one id, raising :class:`TemplateNotFoundError` when it is not stored.
    ``None`` locale resolves the bare id unchanged (the locale-agnostic callers — hooks, authz,
    access-control — read the bare id). A present locale walks the fallback chain and returns the
    first stored variant; when NONE of the chain (variants AND the bare default) is stored, it
    refuses with :class:`TemplateLocaleNotFoundError` naming the template and locale, never a
    silent wrong-language render.
    """
    if locale is None:
        return await compiled(template_id)
    for candidate in locale_variant_ids(template_id, locale):
        try:
            return await compiled(candidate)
        except TemplateNotFoundError:
            continue
    raise TemplateLocaleNotFoundError(
        f"template {template_id!r} has no variant for locale {locale!r} and no default variant"
    )


def with_locale(context: dict[str, Any], locale: str | None) -> dict[str, Any]:
    """The render context with the resolved ``locale`` stamped under the reserved key.

    The ``list_format`` filter reads it — the render carries the language, the template
    never names one. A user variable colliding with the reserved key raises loudly.
    """
    if LOCALE_CONTEXT_KEY in context:
        raise ValueError(f"{LOCALE_CONTEXT_KEY!r} is a reserved render variable and cannot be a template variable")
    return {**context, LOCALE_CONTEXT_KEY: locale}

"""Infer a template's input description from its Jinja source.

Both readers parse with the environment that renders the template, so every registered filter
and extension is known and that environment stays the authority on whether the template parses.
"""

import logging
from typing import Any

from jinja2 import Environment, meta
from jinja2schema import JSONSchemaDraft4Encoder, infer, to_json_schema

logger = logging.getLogger(__name__)

# Vendor marker stamped on a schema that carries variable NAMES without inferred
# types, so a consumer can tell a fully inferred schema from a degraded one
# instead of reading the missing types as "this template takes no typed input".
_INFERENCE_MARKER_KEY = "x-tai42-inference"
_PARTIAL_INFERENCE = "partial"


def _variables_only_schema(env: Environment, content: str) -> dict[str, Any]:
    """Build the degraded, names-only input schema for ``content``.

    The template is parsed by ``env`` — the app's OWN sandboxed environment, the one
    that renders it, so every registered filter and extension is known — and
    each undeclared variable becomes an untyped property. Nothing is marked
    required: the shapes are unknown here, not known-optional.

    Author-broken Jinja raises ``TemplateError`` out of ``parse`` rather than
    degrading to an empty schema: this environment is the authority on whether
    the template PARSES. (Renderability is a stronger property — e.g. an
    unknown Jinja *test* resolves only at render time — and belongs to the
    render door, not to a schema read.)
    """
    names = sorted(meta.find_undeclared_variables(env.parse(content)))
    return {
        "type": "object",
        "properties": {name: {"title": name} for name in names},
        "required": [],
        _INFERENCE_MARKER_KEY: _PARTIAL_INFERENCE,
    }


def template_input_schema(env: Environment, content: str) -> dict[str, Any]:
    """Return the JSON-Schema description of the inputs of the template source ``content``.

    Reading a renderable template never fails. ``jinja2schema`` is the
    enricher: when it can infer the template it returns typed, required-aware
    properties. It supports only a subset of Jinja, though, and rejects plenty
    of valid, renderable templates (dict literals, ``{% with %}``/``{% set %}``
    blocks, ``{% include %}``, filters it does not know) with errors that are
    NOT ``jinja2.TemplateError`` — so any failure of that call degrades to
    :func:`_variables_only_schema`, which carries the variable names and the
    ``x-tai42-inference: partial`` marker.

    Raises ``jinja2.TemplateError`` only when the template itself is broken (``env``
    refuses to parse it) — an author error, not a read failure.
    """
    try:
        return to_json_schema(infer(content), jsonschema_encoder=JSONSchemaDraft4Encoder)
    except Exception as exc:
        # Broad ON PURPOSE and scoped to this one call: jinja2schema signals
        # an unsupported construct as InferException, a bare ``Exception``
        # ("stmt visitor ... not found"), or a ValueError from its own
        # loader. None of those say the template is broken, so none may fail
        # the read — they only mean "no types available".
        logger.debug(
            "jinja2schema could not infer this template (%s: %s); degrading to a variables-only schema.",
            type(exc).__name__,
            exc,
        )
    return _variables_only_schema(env, content)


def undeclared_variables(env: Environment, content: str) -> set[str]:
    """The undeclared variables in the template source ``content``, as ``env`` parses it."""
    return meta.find_undeclared_variables(env.parse(content))

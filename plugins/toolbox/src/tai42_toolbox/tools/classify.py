"""The ``classify`` tool: judge JSON state against typed questions.

Needs the ``classifier`` extra; fails loudly at import when it is absent.
"""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue

try:
    from tai42_kit.llm.classifier import (
        ClassifyQuestion,
        ClassifyRequest,
        ClassifyResponse,
        get_classifier_async,
    )
except ImportError as exc:
    raise ImportError(
        "tai42-toolbox 'classifier' tools require the 'classifier' optional "
        "dependency (tai42-kit[typesafe]). "
        "Install it with: pip install 'tai42-toolbox[classifier]'"
    ) from exc

from tai42_contract.app import tai42_app
from tai42_kit.llm.settings import classifier_settings, llm_provider_settings


@tai42_app.tools.tool(tags={"classifier"})
async def classify(
    state: JsonValue,
    questions: dict[str, ClassifyQuestion],
    classifier_provider: str | None = None,
    classifier_kwargs: dict[str, Any] | None = None,
) -> ClassifyResponse:
    """Judge JSON state against typed questions and return typed answers.

    Each named question is one of three kinds — a binary ``noul`` (the
    probability the answer is yes), a categorical ``choice`` (one label from a
    fixed set, with a probability per label), or an ordinal ``score`` (an
    expected value over an ordered rubric). The response carries one answer per
    question, keyed by the same name, plus token usage and the provider's
    request id.

    ``classifier_kwargs`` keys such as ``base_url`` and ``api_key`` are
    legitimate multi-model routing options, but they are caller-supplied on an
    LLM-fillable tool: expose ``classify`` only to trusted callers, since a
    caller can redirect classification requests to an arbitrary endpoint through
    them.

    Args:
        state: The JSON-compatible state to judge.
        questions: A non-empty mapping of question name to question; each
            question is a ``noul``, ``choice``, or ``score`` discriminated on
            its ``type``.
        classifier_provider: Classifier provider to use; defaults to the
            configured provider.
        classifier_kwargs: Extra provider options; override the configured
            classifier settings.

    Returns:
        One answer per requested question, keyed by the question name, with the
        model, token usage, and provider request id.
    """
    classifier_provider = classifier_provider or llm_provider_settings().classifier
    classifier = await get_classifier_async(
        classifier_provider,
        **classifier_settings().with_fallbacks(classifier_kwargs or {}),
    )
    request = ClassifyRequest(state=state, questions=questions)
    return await classifier.ainvoke(request)

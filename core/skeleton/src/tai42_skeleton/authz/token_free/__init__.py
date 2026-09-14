"""The token-free-evaluable rule for a jq policy condition: which conditions a TOKENLESS
background execution can be authorized against.

A fire's jq context carries the token claims REDUCED to the key's owner, making ``identity``
the SOLE field a condition may not depend on — depending on it is a fail-OPEN, since
``.identity.X != v`` evaluates TRUE under an absent claim. The scan is structural, not by
sampling (``. | tojson`` exfiltrates claims without spelling ``identity``): only
``.identity.owner_user_id``, with nothing further applied, may project ``identity``.

The public entrypoint composes the pipeline in a load-bearing gate order: refuse the
control characters libjq cannot read faithfully first, so the compile gate is never asked
about a NUL-truncated program; then compile, so non-jq text is named as such rather than
refused for its characters; then the rest of the raw-text source-shape gate, so everything
below it works over a condition whose token boundaries jq cannot read differently; then
lex, parse and taint-analyze.
"""

from __future__ import annotations

from tai42_kit.utils.data.jq_util import get_compiled_jq

from .budget import _Budget
from .errors import TokenFreeConditionError
from .lexer import _lex
from .parser import _Parser
from .source_shape import _assert_no_control_characters, _assert_source_shape
from .taint import _TaintAnalysis

__all__ = ["TokenFreeConditionError", "assert_token_free_evaluable"]


def assert_token_free_evaluable(condition_text: str) -> None:
    """Assert that ``condition_text`` can be evaluated for a background execution.

    Returns on an evaluable condition; raises :class:`TokenFreeConditionError` naming the
    offending construct and its offset otherwise, including text that does not compile.
    See the module docstring for the rule and the load-bearing gate order.
    """
    _assert_no_control_characters(condition_text)
    try:
        get_compiled_jq(condition_text)
    except ValueError as exc:
        raise TokenFreeConditionError(
            f"condition does not compile as a jq program ({exc}), so it cannot be shown evaluable for a background "
            "execution"
        ) from exc
    _assert_source_shape(condition_text)
    budget = _Budget()
    tokens = _lex(condition_text, budget)
    program = _Parser(condition_text, tokens, budget).parse()
    _TaintAnalysis(condition_text).assert_safe(program)

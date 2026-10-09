"""The tool-meta pause declaration and the refusal of an undeclared park."""

from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.tools import TOOL_META_PAUSES, UndeclaredPauseError
from tai42_contract.tools.retry import NEVER_RETRYABLE_KINDS


def test_the_key_is_the_pauses_meta_key():
    assert TOOL_META_PAUSES == "tai42/pauses"


def test_the_refusal_names_the_tool_and_the_declaration():
    exc = UndeclaredPauseError("t")
    assert exc.tool == "t"
    assert str(exc) == (
        "tool 't' returned a park signal but does not declare that it can pause; "
        "register it with meta={'tai42/pauses': True}"
    )


def test_the_refusal_is_unknown_and_never_retryable():
    assert error_kind(UndeclaredPauseError("t")) is ErrorKind.UNKNOWN
    assert ErrorKind.UNKNOWN in NEVER_RETRYABLE_KINDS

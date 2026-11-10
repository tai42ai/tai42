"""The errors of an author's jq program, raised on both sides of the jq seam.

Each is the author's input failing, never the platform's transport: they are stamped
``BAD_INPUT`` and carry no transport marker. A failure of the compute pool that runs the
program is the pool's own error and never one of these.
"""

from tai42_contract.errors import ErrorKind


class JqProgramError(ValueError):
    """The author's jq program failed.

    It does not compile, it raised while it ran, it ran past its budget, or it emitted nothing
    where a value is required.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class JqCompileError(JqProgramError):
    """The program does not compile: jq's compile error located in the author's lines, or the seam's refusal."""


class JqEvaluationError(JqProgramError):
    """The compiled program raised while it ran; the message is jq's runtime message verbatim."""


class JqBudgetExceededError(JqProgramError):
    """The program ran past ``JQ_TIMEOUT_SECONDS`` and its worker was killed; the message names the budget."""


class JqNoOutputError(JqProgramError):
    """The program emitted nothing where one value is required."""


__all__ = ["JqBudgetExceededError", "JqCompileError", "JqEvaluationError", "JqNoOutputError", "JqProgramError"]

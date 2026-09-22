"""The ``app.interactions`` facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import _Facet

if TYPE_CHECKING:
    from tai42_contract.interactions import Ask, QuestionFormat


class InteractionsFacet(_Facet):
    """``app.interactions`` — the ``ask`` facade and the shared ``check_answer`` (``AppInteractions``)."""

    def check_answer(self, question: QuestionFormat, answer: Any) -> None:
        """Validate ``answer`` against ``question`` — the ONE answer check every door reaches.

        Delegates to :func:`tai42_skeleton.interactions.answer_check.check_answer`: returns
        ``None`` when the answer conforms, raises ``AnswerMismatchError`` (carrying the failing
        field's dotted path when located) otherwise.
        """
        from tai42_skeleton.interactions.answer_check import check_answer

        check_answer(question, answer)

    async def assert_resume_authorized(self, interaction_id: str) -> None:
        """Authorise the caller to resume/deliver-for ``interaction_id``'s run — or raise.

        Delegates to :func:`tai42_skeleton.interactions.authorization.assert_resume_authorized`:
        passes only inside the platform's own resume of that run, else raises
        ``ParkResumeUnauthorizedError``.
        """
        from tai42_skeleton.interactions.authorization import assert_resume_authorized

        await assert_resume_authorized(interaction_id)

    def assert_delivery_authorized(self, completion_id: str | None) -> None:
        """Authorise a door delivery-address tool to fire for ``completion_id`` — or raise.

        Delegates to :func:`tai42_skeleton.interactions.authorization.assert_delivery_authorized`:
        passes only inside the platform's own delivery fire for that completion, else raises
        ``ParkDeliveryUnauthorizedError``.
        """
        from tai42_skeleton.interactions.authorization import assert_delivery_authorized

        assert_delivery_authorized(completion_id)

    def redelivery_horizon_seconds(self) -> int:
        """The platform's redelivery retention horizon in seconds (the idle retention TTL)."""
        from tai42_skeleton.interactions.authorization import redelivery_horizon_seconds

        return redelivery_horizon_seconds()

    @property
    def ask(self) -> Ask:
        """The bound, ``Ask``-typed ``ask`` callable for an in-process plugin.

        Lets a plugin ask a human without importing the skeleton. A facade EXPOSURE of the
        existing helper — its rich signature and return contract are forwarded verbatim, no
        new ask semantics.
        """
        from tai42_skeleton.interactions.helper import ask

        return ask

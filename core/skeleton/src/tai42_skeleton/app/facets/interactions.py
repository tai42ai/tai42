"""The ``app.interactions`` facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import _Facet

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from tai42_contract.interactions import Ask, ParkedEntry, QuestionFormat, Visit, VisitOutcome
    from tai42_contract.states import StateContext


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
    def visit(self) -> Visit:
        """The bound, ``Visit``-typed shared-visit callable — the ONE seam every door drives through.

        A facade EXPOSURE of :func:`tai42_skeleton.interactions.visit.visit`, so an in-process door
        or plugin drives a parkable run's cancel / resume / take / start order without importing the
        skeleton.
        """
        from tai42_skeleton.interactions.visit import visit

        return visit

    def park_answer(self, outcome: VisitOutcome) -> Any:
        """The ONE park-answer shape a direct door hands back for a visit outcome.

        A facade EXPOSURE of :func:`tai42_skeleton.interactions.visit.park_answer`: the caller ask
        entries, the suspended sentinel, the tool's own result, or ``None`` — never revealing a
        wrapped secret (the sync door reveals those on the ``result`` kind itself).
        """
        from tai42_skeleton.interactions.visit import park_answer

        return park_answer(outcome)

    async def normalise_started(self, value: Any) -> VisitOutcome:
        """Classify a raw start return into a ``started`` :class:`VisitOutcome` over the ambient subject.

        A facade EXPOSURE of :func:`tai42_skeleton.interactions.visit.normalise_started`, for a door
        that ran its start inside its OWN visit and only needs the return classified.
        """
        from tai42_skeleton.interactions.visit import normalise_started

        return await normalise_started(value)

    async def list_parked(self) -> list[ParkedEntry]:
        """Every parked interaction on the current run's subject — the full parked entries."""
        from tai42_skeleton.interactions.visit import list_parked

        return await list_parked()

    async def list_parked_for(self, context: StateContext | None) -> list[ParkedEntry]:
        """Every parked interaction on ``context``'s subject — the door-contract ``$parked`` source."""
        from tai42_skeleton.interactions.visit import list_parked_for

        return await list_parked_for(context)

    def current_fire_identity(self) -> tuple[str, str] | None:
        """The ambient execution identity as the ``(user_id, fingerprint)`` pair a fire forwards, or ``None``."""
        from tai42_skeleton.authz.execution_identity import get_execution_identity

        identity = get_execution_identity()
        if identity is None or identity.user_id is None or identity.execution_key_fingerprint is None:
            return None
        return identity.user_id, identity.execution_key_fingerprint

    def bound_execution_identity_for_fire(
        self, execution_key: str, fingerprint: str
    ) -> AbstractAsyncContextManager[Any]:
        """Bind ``execution_key``'s live-grant identity for a receiver-less door fire (a scheduled fire)."""
        from tai42_skeleton.authz.execution import bind_execution_identity

        return bind_execution_identity(execution_key, bound_fingerprint=fingerprint)

    async def resume_parked(self, interaction_id: str, payload: Any = ...) -> VisitOutcome:
        """Resume caller ask ``interaction_id`` with ``payload``, or TAKE its waiting outcome when omitted."""
        from tai42_skeleton.interactions.visit import _TAKE, resume_parked

        return await resume_parked(interaction_id, _TAKE if payload is ... else payload)

    async def cancel_parked(self, ids: list[str]) -> VisitOutcome:
        """Whole-chain kill every parked interaction named in ``ids`` on the current run's subject."""
        from tai42_skeleton.interactions.visit import cancel_parked

        return await cancel_parked(ids)

    @property
    def ask(self) -> Ask:
        """The bound, ``Ask``-typed ``ask`` callable for an in-process plugin.

        Lets a plugin ask a human without importing the skeleton. A facade EXPOSURE of the
        existing helper — its rich signature and return contract are forwarded verbatim, no
        new ask semantics.
        """
        from tai42_skeleton.interactions.helper import ask

        return ask

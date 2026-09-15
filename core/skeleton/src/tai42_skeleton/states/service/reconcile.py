"""The platform's own attach reconciler and the reconcile engine.

On a declarations edit of a template that declares ``reconcile``, the platform settles the
state's open records against the new declarations through the template's ``reconcile``
contract — collecting orphaned items across the state, then (with the operator's directive)
closing them with the template's ``close`` jq. No template concept enters this body.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.states.errors import TemplateValidationError
from tai42_contract.states.models import AttachReconcileContext, StateSubject, StateTemplateReconcile
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.jq_util import run_jq_first

from tai42_skeleton.states.service.base import _StatesServiceBase
from tai42_skeleton.states.service.reconcile_support import (
    _RECONCILE_ORIGIN,
    _RECONCILE_PAGE,
    _rebase_op,
    _reconcile_orphans_extra,
    _reconcile_refusal,
    _record_subtree,
)
from tai42_skeleton.states.templates import validate_template
from tai42_skeleton.template.resource_manager import TemplateLocaleNotFoundError, TemplateNotFoundError


class _ReconcileMixin(_StatesServiceBase):
    async def _reconcile_template_records(self, context: AttachReconcileContext) -> None:
        """The platform's own attach reconciler (always registered).

        On a declarations edit of a template that declares ``reconcile``, it settles the state's open records against
        the new declarations through the template's ``reconcile`` contract — a no-op on a first
        attach (no previous declarations) or a template without ``reconcile``. No template concept
        enters this body: the template's own jq decides what a declarations edit orphans and how to close it.
        """
        if context.previous_declarations is None:
            return
        resolved_body, _by_id = await self._resolve_template_body(
            context.template.name, context.template.model_dump(by_alias=True, exclude_none=True)
        )
        template = validate_template(resolved_body)
        if template.reconcile is None:
            return
        path = await self._reconcile_attach_path(context.state, context.template.name)
        await self._run_reconcile(context, template.reconcile, path)

    async def _reconcile_attach_path(self, state: str, template_name: str) -> list[str]:
        """The path at which ``template_name`` is attached on ``state`` — the subtree its records live under.

        The attachment exists at reconcile time (a re-attach / declarations edit).
        """
        for template, attach_path, _params, _decls in await self._load_state_attachments(state):
            if template.name == template_name:
                return list(attach_path)
        raise RuntimeError(f"reconcile: template {template_name!r} is not attached on state {state!r}")

    async def _run_reconcile(
        self, context: AttachReconcileContext, reconcile: StateTemplateReconcile, path: list[str]
    ) -> None:
        orphans = await self._collect_reconcile_orphans(context, reconcile, path)
        if not orphans:
            return
        await self._close_reconcile_orphans(context, reconcile, path, orphans)

    async def _collect_reconcile_orphans(
        self, context: AttachReconcileContext, reconcile: StateTemplateReconcile, path: list[str]
    ) -> list[tuple[StateSubject, dict[str, Any]]]:
        """The keyset-paged scan gathering the items the new declarations no longer cover.

        Reads each subject's subtree and returns ``(subject, item)`` pairs.
        """
        previous = context.previous_declarations or {}
        new = context.new_declarations
        orphans: list[tuple[StateSubject, dict[str, Any]]] = []
        cursor: str | None = None
        while True:
            page = await context.records.list_subjects(limit=_RECONCILE_PAGE, cursor=cursor)
            for entry in page["subjects"]:
                subject = StateSubject(**entry["subject"])
                view = await context.records.read(subject)
                if view is None:
                    continue
                orphans.extend(
                    (subject, item)
                    for item in await self._reconcile_orphans(
                        reconcile,
                        _record_subtree(view.data, path),
                        previous=previous,
                        new=new,
                        template_name=context.template.name,
                    )
                )
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        return orphans

    async def _close_reconcile_orphans(
        self,
        context: AttachReconcileContext,
        reconcile: StateTemplateReconcile,
        path: list[str],
        orphans: list[tuple[StateSubject, dict[str, Any]]],
    ) -> None:
        """Read the ``orphans`` directive, guard the resolution, run the ``close`` jq per orphan, and apply the ops.

        A missing directive is the loud refusal listing the orphans.
        """
        directive = context.options.get("orphans")
        if directive is None:
            raise TemplateValidationError(_reconcile_refusal(context, orphans), extra=_reconcile_orphans_extra(orphans))
        if directive != "close":
            raise TemplateValidationError(
                f"re-attaching template {context.template.name!r} on state {context.state!r}: unknown reconcile "
                f'directive options.orphans={directive!r}; the only directive is "close"'
            )
        resolution = context.options.get("resolution")
        await self._reconcile_guard_resolution(context, reconcile, context.new_declarations, resolution)
        for subject, item in orphans:
            current = await context.records.read(subject)
            subtree = _record_subtree(current.data, path) if current is not None else {}
            ops = await self._run_reconcile_jq(
                "close",
                reconcile.close,
                {"data": subtree, "id": item["id"], "resolution": resolution},
                template_name=context.template.name,
            )
            if not isinstance(ops, list):
                raise TemplateValidationError(f"reconcile close must return a list of ops, got {type(ops).__name__}")
            await context.records.apply(subject, [_rebase_op(op, path) for op in ops], origin=_RECONCILE_ORIGIN)

    async def _reconcile_orphans(
        self,
        reconcile: StateTemplateReconcile,
        subtree: dict[str, Any],
        *,
        previous: dict[str, Any],
        new: dict[str, Any],
        template_name: str,
    ) -> list[dict[str, Any]]:
        result = await self._run_reconcile_jq(
            "orphans",
            reconcile.orphans,
            {"previous": previous, "new": new, "data": subtree},
            template_name=template_name,
        )
        if not isinstance(result, list):
            raise TemplateValidationError(
                f"reconcile orphans must return a list of {{id, label}}, got {type(result).__name__}"
            )
        return result

    async def _reconcile_guard_resolution(
        self, context: AttachReconcileContext, reconcile: StateTemplateReconcile, new: dict[str, Any], resolution: Any
    ) -> None:
        if not isinstance(resolution, str) or not resolution.strip():
            raise TemplateValidationError(
                f"re-attaching template {context.template.name!r} on state {context.state!r}: "
                'options.orphans="close" needs options.resolution naming a not-done resolution'
            )
        declared = await self._run_reconcile_jq(
            "resolutions", reconcile.resolutions, {"new": new}, template_name=context.template.name
        )
        names = declared if isinstance(declared, list) else []
        if resolution not in names:
            raise TemplateValidationError(
                f"re-attaching template {context.template.name!r} on state {context.state!r}: resolution "
                f"{resolution!r} is not a not-done resolution the new declarations declare "
                f"(declared: {sorted(str(n) for n in names)})"
            )

    async def _run_reconcile_jq(self, label: str, text: TemplatedText, payload: Any, *, template_name: str) -> Any:
        """One reconcile jq program over its input payload.

        Its body is a templated text rendered to jq text IMMEDIATELY before it runs — a by-id body whose stored
        resource cannot be fetched is a LOUD refusal naming the program and the id. Loud, too, on an evaluation
        failure, carrying the program's own ``error(...)`` message out.
        """
        try:
            expr = await tai42_app.storage.resource_manager.render_templated_text(text)
        except (TemplateNotFoundError, TemplateLocaleNotFoundError) as exc:
            raise TemplateValidationError(
                f"template {template_name!r} reconcile {label} references stored id {text.id!r}, "
                f"which could not be fetched: {exc}"
            ) from exc
        try:
            return await run_jq_first(expr, payload)
        except Exception as exc:
            raise TemplateValidationError(f"reconcile {label} failed to evaluate: {exc}") from exc

"""Resolving a subject reference into the full subject a state's record doors address.

One rule set every subject-addressing caller shares: a full mapping, a ``{kind, key}`` mapping
under the ambient context's target, a key string under the state's ``default_subject_kind``, or
nothing at all (the ambient candidate for that kind). Every reference that cannot be resolved is a
loud :class:`SubjectRefusedError` naming the rule — never an unaddressed write.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError
from tai42_contract.states.errors import StateNotFoundError, SubjectRefusedError
from tai42_contract.states.models import StateContext, StateSubject

from tai42_skeleton.states.service.base import _StatesServiceBase


def _validated(state: str, ref: Mapping[str, Any], fields: Mapping[str, Any]) -> StateSubject:
    """``fields`` validated as a :class:`StateSubject`; a refusal names the reference and each failing field."""
    try:
        return StateSubject.model_validate(dict(fields))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise SubjectRefusedError(f"state {state!r}: subject {dict(ref)!r} is not a valid subject: {problems}") from exc


def _from_mapping(state: str, ref: Mapping[str, Any], ctx: StateContext | None) -> StateSubject:
    """A full subject mapping as is; a ``{kind, key}`` mapping under the ambient context's target."""
    has_target_kind = "target_kind" in ref
    if has_target_kind != ("target_name" in ref):
        raise SubjectRefusedError(
            f"state {state!r}: an explicit subject must give both target_kind and target_name or neither"
        )
    if has_target_kind:
        return _validated(state, ref, ref)
    if ctx is None:
        raise SubjectRefusedError(
            f"state {state!r}: subject {dict(ref)!r} names no target and no ambient context is in scope "
            "to supply one — give target_kind and target_name"
        )
    target = {"target_kind": ctx.candidates.target_kind, "target_name": ctx.candidates.target_name}
    return _validated(state, ref, {**target, **ref})


def _ambient_key(state: str, ref: str | None, default_kind: str, ctx: StateContext | None) -> tuple[str, StateContext]:
    """The key a key string or an omitted subject names, with the ambient context its target comes from."""
    if ref is None:
        if ctx is None:
            raise SubjectRefusedError(f"state {state!r}: no subject in scope: pass subject explicitly")
        candidate = ctx.candidates.by_kind.get(default_kind)
        if candidate is None:
            raise SubjectRefusedError(
                f"state {state!r}: the ambient {ctx.door!r} door resolved no subject of kind "
                f"{default_kind!r} (the state's default_subject_kind) — pass subject explicitly"
            )
        return candidate, ctx
    if not ref.strip():
        raise SubjectRefusedError(f"state {state!r}: a subject key must be a non-empty string, got {ref!r}")
    if ctx is None:
        raise SubjectRefusedError(
            f"state {state!r}: subject {ref!r} names no target and no ambient context is in scope "
            "to supply one — give target_kind and target_name"
        )
    return ref, ctx


class _SubjectMixin(_StatesServiceBase):
    async def resolve_subject(self, state: str, ref: str | Mapping[str, Any] | None) -> StateSubject:
        """Resolve the subject reference ``ref`` for ``state``.

        A mapping with both ``target_kind`` and ``target_name`` is validated as the full subject;
        a mapping with one of them is refused; a mapping with neither (``{kind, key}``) takes its
        target from the ambient context. A non-blank key string takes the state's
        ``default_subject_kind`` and the ambient target; ``None`` takes the ambient candidate for
        that kind. No ambient context where one is needed, a blank key, a missing candidate, a
        mapping that does not validate, or any other shape raises :class:`SubjectRefusedError`.

        Only a key string and ``None`` read the declaration (for its ``default_subject_kind``);
        on an undeclared state they raise :class:`StateNotFoundError`. A mapping resolves with no
        store read: the record door it addresses admits the subject against the declaration and
        refuses an undeclared state there.
        """
        self._ensure_available()
        ctx = self.context()
        if isinstance(ref, Mapping):
            return _from_mapping(state, ref, ctx)
        if ref is not None and not isinstance(ref, str):
            raise SubjectRefusedError(
                f"state {state!r}: a subject is a subject object, a key string or omitted, got {ref!r}"
            )
        scalars = await self._store.declaration_scalars(state)
        if scalars is None:
            raise StateNotFoundError(f"no state declared as {state!r}")
        default_kind = scalars[2]
        key, ambient = _ambient_key(state, ref, default_kind, ctx)
        fields = {
            "target_kind": ambient.candidates.target_kind,
            "target_name": ambient.candidates.target_name,
            "kind": default_kind,
            "key": key,
        }
        return _validated(state, fields, fields)

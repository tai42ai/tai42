"""The canonical wire shape of :class:`~tai42_contract.template.TemplatedText`, pinned at
the two doors that serialize it differently before the type owned its own shape: the hooks
door (``list_hooks`` dumps ``HookParams`` with ``mode="json"``) and the presets door (the
store persists ``PresetBody.model_dump()``). Both must emit exactly the ONE source key that
is set and ``kwargs`` only when it carries parameters — no null source, no empty-kwargs
noise — so a strict consumer accepts either door's output and the two cannot drift apart.
"""

from __future__ import annotations

from tai42_contract.hooks import HookParams
from tai42_contract.presets.models import PresetBody
from tai42_contract.template import TemplatedText

_BY_ID = TemplatedText(id="quota", kwargs={"tier": "pro"})
_INLINE = TemplatedText(content=".subject_id")

_CANONICAL_BY_ID = {"id": "quota", "kwargs": {"tier": "pro"}}
_CANONICAL_INLINE = {"content": ".subject_id"}


def _hook_params(**over: object) -> HookParams:
    return HookParams(
        name="on-alert",
        topic="alerts",
        tool="notify",
        execution_key="key-user",
        execution_key_fingerprint="fp-1",
        **over,  # type: ignore[arg-type]
    )


def test_hooks_door_serializes_templated_text_canonically() -> None:
    # ``list_hooks`` dumps each stored ``HookParams`` with ``mode="json"``; the nested
    # condition/expr must carry exactly their set source key and no null.
    dumped = _hook_params(condition=_BY_ID, expr=_INLINE).model_dump(mode="json")
    assert dumped["condition"] == _CANONICAL_BY_ID
    assert dumped["expr"] == _CANONICAL_INLINE


def test_presets_door_serializes_templated_text_canonically() -> None:
    # The presets store persists ``PresetBody.model_dump()``; a by-id / inline schema body
    # must serialize to the same single-source shape.
    dumped = PresetBody(base_tool="echo", output_schema=_BY_ID, input_schema=_INLINE).model_dump()
    assert dumped["output_schema"] == _CANONICAL_BY_ID
    assert dumped["input_schema"] == _CANONICAL_INLINE


def test_the_two_doors_emit_the_identical_shape() -> None:
    # The anti-drift pin: the hooks door and the presets door serialize the SAME
    # ``TemplatedText`` to the SAME bytes.
    hook = _hook_params(condition=_BY_ID, expr=_INLINE).model_dump(mode="json")
    preset = PresetBody(base_tool="echo", output_schema=_BY_ID, input_schema=_INLINE).model_dump(mode="json")
    assert hook["condition"] == preset["output_schema"]
    assert hook["expr"] == preset["input_schema"]

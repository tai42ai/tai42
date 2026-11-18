"""The template door answers the template layer's typed refusals with the operation errors the adapter maps."""

from __future__ import annotations

import pytest

from tai42_skeleton.operations import ConflictError, NotSupportedError, OperationFailedError
from tai42_skeleton.operations.holders import holders_refusal
from tai42_skeleton.operations.template_door import template_door
from tai42_skeleton.template import FilesNotConfiguredError, HolderUnreadableError, StoredObjectHeldError
from tai42_skeleton.tools import holder_kinds

from .._fakes.caller_reach import bound_caller
from .._fakes.template_holders import HELD_HOLDERS, HELD_TEMPLATE_REFUSAL, HOOK_HOLDER, PRESET_HOLDER

_HEAD = "stored template 'jq/gate' cannot be deleted"


async def test_a_held_object_is_the_409_holders_refusal_words(monkeypatch: pytest.MonkeyPatch) -> None:
    with bound_caller(monkeypatch, admin=True), pytest.raises(ConflictError) as caught:
        async with template_door():
            raise StoredObjectHeldError(_HEAD, [HOOK_HOLDER, PRESET_HOLDER])
    assert str(caught.value) == HELD_TEMPLATE_REFUSAL
    assert caught.value.status == 409
    assert caught.value.extra == {"holders": HELD_HOLDERS}
    assert isinstance(caught.value.__cause__, StoredObjectHeldError)


async def test_a_held_objects_refusal_is_the_callers_view(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = holder_kinds.HOOK.holder(name="nightly", summary="hook 'nightly' names 'jq/gate'")
    with bound_caller(monkeypatch, {"presets": "write"}):
        expected = await holders_refusal(_HEAD, [hook], "remove those references first")
        with pytest.raises(ConflictError) as caught:
            async with template_door():
                raise StoredObjectHeldError(_HEAD, [hook])
    assert str(caught.value) == str(expected)
    assert caught.value.extra == expected.extra
    assert "nightly" not in str(caught.value)


async def test_an_unreadable_holder_is_the_500_with_the_same_text() -> None:
    fault = ValueError("bad value 'x'")
    with pytest.raises(OperationFailedError) as caught:
        async with template_door():
            raise HolderUnreadableError(_HEAD, type(fault).__name__) from fault
    assert str(caught.value) == f"{_HEAD}: a holder could not be read (ValueError)"
    assert caught.value.status == 500


async def test_a_missing_files_extra_is_the_501_with_its_code() -> None:
    with pytest.raises(NotSupportedError) as caught:
        async with template_door():
            raise FilesNotConfiguredError("install the files extra")
    assert str(caught.value) == "install the files extra"
    assert caught.value.status == 501
    assert caught.value.extra == {"code": "files-not-configured"}


async def test_any_other_error_passes_through_as_it_is() -> None:
    with pytest.raises(FileNotFoundError, match="gone"):
        async with template_door():
            raise FileNotFoundError("gone")

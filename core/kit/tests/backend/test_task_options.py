"""The branch tools' task options and the split taking them out of a dispatch's kwargs."""

from __future__ import annotations

from typing import Any

from tai42_contract.backend import SCHEDULE_CADENCE_KEY, SCHEDULE_NAME_KEY

from tai42_kit.backend import (
    BACKEND_TASK_OPTS,
    CALLBACK_OPTION,
    SCHEDULE_OPTS,
    CallbackSchema,
    split_task_options,
    take_callback,
)
from tai42_kit.utils.schedule_subject import SCHEDULE_SUBJECT_ARG
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG


def test_every_backend_honours_the_same_task_options() -> None:
    assert set(BACKEND_TASK_OPTS) == {"countdown", "expires", "eta", CALLBACK_OPTION, "queue"}
    assert BACKEND_TASK_OPTS[CALLBACK_OPTION] == CallbackSchema | None


def test_the_schedule_options_are_the_name_and_the_cadence() -> None:
    assert set(SCHEDULE_OPTS) == {SCHEDULE_NAME_KEY, SCHEDULE_CADENCE_KEY}


def test_split_takes_the_options_out_and_keeps_only_given_values() -> None:
    kwargs: dict[str, Any] = {"x": 1, "countdown": 0, "eta": None, "queue": "gpu"}
    options = split_task_options(kwargs, BACKEND_TASK_OPTS)
    assert options == {"countdown": 0, "queue": "gpu"}
    assert kwargs == {"x": 1}


def test_split_leaves_kwargs_alone_when_no_option_rode_along() -> None:
    kwargs: dict[str, Any] = {"x": 1}
    assert split_task_options(kwargs, BACKEND_TASK_OPTS) == {}
    assert kwargs == {"x": 1}


def test_take_callback_pops_it_and_carries_the_forwarded_pair(gate_declared: None) -> None:
    callback = CallbackSchema(tool="follow_up")
    options: dict[str, Any] = {CALLBACK_OPTION: callback, "countdown": 5}
    taken = take_callback(options, {"x": 1, SCHEDULE_SUBJECT_ARG: {"kind": "s"}})
    assert taken is callback
    assert options == {"countdown": 5}
    assert callback.carried_kwargs[SCHEDULE_SUBJECT_ARG] == {"kind": "s"}
    assert WORKER_SECRET_CAPABILITY_ARG in callback.carried_kwargs


def test_take_callback_validates_a_mapping_into_the_schema(gate_declared: None) -> None:
    taken = take_callback({CALLBACK_OPTION: {"tool": "follow_up"}}, {})
    assert isinstance(taken, CallbackSchema)
    assert taken.tool == "follow_up"


def test_take_callback_is_none_without_one() -> None:
    options: dict[str, Any] = {"countdown": 5}
    assert take_callback(options, {}) is None
    assert options == {"countdown": 5}

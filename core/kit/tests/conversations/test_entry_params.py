"""The entry-params policy: REFUSE re-raises the bound, DROP keeps the turn; every drop is logged and counted."""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest
from tai42_contract.entry_params import ENTRY_PARAM_VALUE_MAX_CHARS, ENTRY_PARAMS_MAX_COUNT, validate_entry_params

from tai42_kit.conversations.entry_params import OverBoundParamsMode, bound_entry_params, put_entry_param

_LOGGER = "tai42_kit.conversations.entry_params"
_SECRET = "s3cret-value-never-logged"


def _dropped(door: str, unit: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value("tai_entry_params_dropped_total", {"door": door, "unit": unit}) or 0.0


def _over_count() -> dict[str, str]:
    return {f"k{i}": _SECRET for i in range(ENTRY_PARAMS_MAX_COUNT + 1)}


@pytest.mark.parametrize("params", [None, {}])
def test_no_params_carry_on_as_none(params: dict[str, str] | None) -> None:
    for mode in OverBoundParamsMode:
        assert bound_entry_params(params, mode=mode, door="probe") is None


def test_params_within_the_bounds_carry_on_as_a_copy() -> None:
    params = {"source": "relay", "lang": "en"}
    for mode in OverBoundParamsMode:
        carried = bound_entry_params(params, mode=mode, door="probe")
        assert carried == params
        assert carried is not params


def test_refuse_re_raises_the_bound_text_unchanged_and_counts_nothing(caplog: pytest.LogCaptureFixture) -> None:
    params = _over_count()
    with pytest.raises(ValueError, match="keys, over the") as expected:
        validate_entry_params(dict(params))
    before = _dropped("probe-refuse", "set")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER), pytest.raises(ValueError, match="keys, over the") as refused:
        bound_entry_params(params, mode=OverBoundParamsMode.REFUSE, door="probe-refuse")

    assert str(refused.value) == str(expected.value)
    assert _SECRET not in str(refused.value)
    assert caplog.records == []
    assert _dropped("probe-refuse", "set") == before


def test_drop_returns_none_logs_one_warning_and_counts_one_set(caplog: pytest.LogCaptureFixture) -> None:
    before = _dropped("probe-drop", "set")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        carried = bound_entry_params(_over_count(), mode=OverBoundParamsMode.DROP, door="probe-drop")

    assert carried is None
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.WARNING,
            f"probe-drop entry params dropped (params carries {ENTRY_PARAMS_MAX_COUNT + 1} keys, over the "
            f"{ENTRY_PARAMS_MAX_COUNT} allowed); the turn proceeds without params",
        )
    ]
    assert _dropped("probe-drop", "set") == before + 1


def test_a_dropped_set_never_logs_a_value(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        bound_entry_params({"k": _SECRET * 40}, mode=OverBoundParamsMode.DROP, door="probe-drop")
    assert len(caplog.records) == 1
    assert _SECRET not in caplog.text


def test_put_adds_a_value_within_the_cap() -> None:
    params: dict[str, str] = {}
    put_entry_param(params, "k", "a" * ENTRY_PARAM_VALUE_MAX_CHARS, door="probe")
    assert params == {"k": "a" * ENTRY_PARAM_VALUE_MAX_CHARS}


@pytest.mark.parametrize("value", [None, "", 7, b"bytes", ["v"]])
def test_put_leaves_an_empty_or_non_string_value_absent_silently(
    value: object, caplog: pytest.LogCaptureFixture
) -> None:
    params: dict[str, str] = {}
    before = _dropped("probe-absent", "value")
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        put_entry_param(params, "k", value, door="probe-absent")
    assert params == {}
    assert caplog.records == []
    assert _dropped("probe-absent", "value") == before


def test_put_drops_an_over_cap_value_with_a_warning_and_one_count(caplog: pytest.LogCaptureFixture) -> None:
    params: dict[str, str] = {}
    before = _dropped("probe-value", "value")
    over_cap = _SECRET + "x" * ENTRY_PARAM_VALUE_MAX_CHARS

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        put_entry_param(params, "media_id", over_cap, door="probe-value")

    assert params == {}
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.WARNING,
            f"probe-value entry param 'media_id' dropped: value over the {ENTRY_PARAM_VALUE_MAX_CHARS}-character limit",
        )
    ]
    assert _SECRET not in caplog.text
    assert _dropped("probe-value", "value") == before + 1


def test_the_counter_is_served_by_the_default_registry_exposition() -> None:
    from prometheus_client import REGISTRY, generate_latest

    put_entry_param({}, "k", "x" * (ENTRY_PARAM_VALUE_MAX_CHARS + 1), door="probe-served")

    exposition = generate_latest(REGISTRY).decode()
    assert 'tai_entry_params_dropped_total{door="probe-served",unit="value"}' in exposition


def test_importing_the_module_does_not_import_the_metrics_library() -> None:
    probe = (
        "import sys, tai42_kit.conversations.entry_params, tai42_kit.conversations.inbound_media; "
        "print('prometheus_client' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"

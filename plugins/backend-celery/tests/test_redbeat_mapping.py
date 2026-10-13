"""The ``ScheduleCadence`` ↔ RedBeat mapping, checked against RedBeat's own encoder and decoder."""

from __future__ import annotations

import json

import pytest
from celery.schedules import crontab, schedule
from redbeat.decoder import RedBeatJSONDecoder, RedBeatJSONEncoder
from tai42_contract.backend import CrontabCadence, IntervalCadence

from tai42_backend_celery.core.redbeat_mapping import from_redbeat, to_celery_schedule, to_redbeat

_CADENCES = [
    IntervalCadence(every=30),
    IntervalCadence(every=30, relative=True),
    CrontabCadence(minute="*/5", hour="1", day_of_week="2"),
]


@pytest.mark.parametrize("cadence", _CADENCES)
def test_to_redbeat_equals_the_encoder_output(cadence: IntervalCadence | CrontabCadence) -> None:
    encoded = json.dumps(to_celery_schedule(cadence), cls=RedBeatJSONEncoder)
    assert to_redbeat(cadence) == json.loads(encoded)
    # Byte-identical, key order included, so an entry the mapper writes reads as one RedBeat wrote.
    assert json.dumps(to_redbeat(cadence)) == encoded


@pytest.mark.parametrize("cadence", _CADENCES)
def test_from_redbeat_round_trips_an_encoder_written_definition(cadence: IntervalCadence | CrontabCadence) -> None:
    stored = json.loads(json.dumps(to_celery_schedule(cadence), cls=RedBeatJSONEncoder))
    assert from_redbeat(stored) == cadence


@pytest.mark.parametrize("cadence", _CADENCES)
def test_the_decoder_reads_the_mapper_output(cadence: IntervalCadence | CrontabCadence) -> None:
    decoded = json.loads(json.dumps(to_redbeat(cadence)), cls=RedBeatJSONDecoder)
    expected = to_celery_schedule(cadence)
    assert type(decoded) is type(expected)
    if isinstance(expected, crontab):
        assert decoded == expected
    else:
        assert isinstance(decoded, schedule)
        assert not isinstance(decoded, crontab)
        assert decoded.run_every == expected.run_every
        assert decoded.relative == expected.relative


def test_from_redbeat_refuses_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown schedule type 'rrule'"):
        from_redbeat({"__type__": "rrule", "freq": 1})

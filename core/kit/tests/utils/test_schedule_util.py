import pytest

from tai42_kit.utils.runtime.schedule_util import normalize_schedule, parse_crontab_expr


class TestParseCrontabExpr:
    def test_five_field(self):
        out = parse_crontab_expr("*/5 1 2 3 4")
        assert out == {
            "type": "crontab",
            "minute": "*/5",
            "hour": "1",
            "day_of_month": "2",
            "month_of_year": "3",
            "day_of_week": "4",
        }

    def test_six_field_drops_seconds(self):
        out = parse_crontab_expr("30 0 12 1 2 3")
        assert out["minute"] == "0"
        assert out["hour"] == "12"
        assert out["day_of_week"] == "3"

    def test_wrong_field_count_raises(self):
        with pytest.raises(ValueError, match="need 5 fields"):
            parse_crontab_expr("1 2 3")


class TestNormalizeSchedule:
    def test_int_to_interval_seconds(self):
        assert normalize_schedule(30) == {"__type__": "interval", "every": 30.0, "relative": False}

    def test_float_to_interval_seconds(self):
        out = normalize_schedule(2.5)
        assert out["__type__"] == "interval"
        assert out["every"] == 2.5

    def test_non_positive_interval_raises(self):
        with pytest.raises(ValueError, match="must be > 0"):
            normalize_schedule(0)

    def test_canonical_interval_dict_normalized(self):
        out = normalize_schedule({"__type__": "interval", "every": 10})
        assert out["every"] == 10.0
        assert out["relative"] is False

    def test_canonical_interval_non_positive_raises(self):
        with pytest.raises(ValueError, match="must be > 0"):
            normalize_schedule({"__type__": "interval", "every": -1})

    def test_bare_string_is_crontab(self):
        out = normalize_schedule("*/5 * * * *")
        assert out["__type__"] == "crontab"
        assert out["minute"] == "*/5"

    def test_canonical_crontab_dict_passes_through(self):
        # An already-canonical crontab dict is returned unchanged (no interval
        # float-coercion branch).
        canonical = {"__type__": "crontab", "minute": "0", "hour": "9", "day_of_week": "1"}
        assert normalize_schedule(dict(canonical)) == canonical

    def test_friendly_interval_with_period(self):
        out = normalize_schedule({"type": "interval", "every": 5, "period": "minutes"})
        assert out == {"__type__": "interval", "every": 300.0, "relative": False}

    def test_friendly_interval_run_every_alias(self):
        out = normalize_schedule({"type": "interval", "run_every": 2, "period": "hours"})
        assert out["every"] == 7200.0

    def test_friendly_interval_missing_every_raises(self):
        with pytest.raises(ValueError, match="requires 'every'"):
            normalize_schedule({"type": "interval"})

    def test_friendly_interval_bad_period_raises(self):
        with pytest.raises(ValueError, match="Unsupported period"):
            normalize_schedule({"type": "interval", "every": 1, "period": "fortnights"})

    def test_friendly_interval_non_positive_after_scaling_raises(self):
        with pytest.raises(ValueError, match="must be > 0"):
            normalize_schedule({"type": "interval", "every": -5, "period": "seconds"})

    def test_friendly_crontab_fields(self):
        out = normalize_schedule({"type": "crontab", "minute": "0", "hour": "9"})
        assert out["__type__"] == "crontab"
        assert out["minute"] == "0"
        assert out["hour"] == "9"
        assert out["day_of_month"] == "*"

    def test_friendly_crontab_expression_with_override(self):
        out = normalize_schedule({"type": "crontab", "expression": "0 0 * * *", "hour": "6"})
        assert out["minute"] == "0"
        assert out["hour"] == "6"

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError, match="Unsupported schedule format"):
            normalize_schedule(["not", "valid"])  # type: ignore[arg-type]

    def test_unknown_dict_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported schedule format"):
            normalize_schedule({"type": "bogus"})

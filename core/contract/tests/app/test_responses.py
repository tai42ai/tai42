"""The profile-apply response models: a recycle line carries an optional ``detail``."""

from __future__ import annotations

from tai42_contract.app.responses import ProfileApplyResponse, RecycleEntry, RecycleStop


def test_a_recycle_line_round_trips_with_and_without_a_detail() -> None:
    recycled = RecycleEntry(name="backend-1", kind="backend", status="recycled", generation_before=1)
    assert recycled.detail is None
    assert RecycleEntry.model_validate(recycled.model_dump()) == recycled

    stopped = RecycleEntry(
        name="backend-2", kind="backend", status="timed-out", generation_before=3, detail="old life still present"
    )
    assert RecycleEntry.model_validate_json(stopped.model_dump_json()) == stopped


def test_a_recycle_line_without_a_detail_key_validates() -> None:
    entry = RecycleEntry.model_validate(
        {"name": "serve-1", "kind": "serve", "status": "self-deferred", "generation_before": 7}
    )
    assert entry.detail is None


def test_the_detail_is_optional_in_the_schema() -> None:
    schema = ProfileApplyResponse.model_json_schema()["$defs"]["RecycleEntry"]
    assert "detail" in schema["properties"]
    assert "detail" not in schema["required"]


def test_a_recycle_stop_round_trips_with_and_without_a_target() -> None:
    no_target = RecycleStop(kind="backend", name=None, detail="bus unreachable while reading the census")
    assert RecycleStop.model_validate_json(no_target.model_dump_json()) == no_target
    with_target = RecycleStop(kind="backend", name="backend-1", detail="old life still present")
    assert RecycleStop.model_validate(with_target.model_dump()) == with_target


def test_the_profile_apply_response_defaults_recycle_stopped_to_none() -> None:
    body = ProfileApplyResponse.model_validate(
        {"hot": [], "recycle": [], "fresh": [], "refused": [], "fanout": {"mode": "fleet"}}
    )
    assert body.recycle_stopped is None
    schema = ProfileApplyResponse.model_json_schema()
    assert "recycle_stopped" in schema["properties"]
    assert "recycle_stopped" not in schema["required"]


def test_an_apply_response_carries_the_recycle_roll_and_the_keys_awaiting_a_respawn() -> None:
    from tai42_contract.app.responses import ApplyResponse, RecycleRoll

    body = ApplyResponse.model_validate(
        {
            "status": "ok",
            "env_keys": 3,
            "awaiting_respawn": [{"name": "backend-2", "keys": ["BACKEND_MANIFEST_KEY"]}],
            "fanout": {"mode": "fleet"},
            "recycle": {
                "recycle": [{"name": "backend-1", "kind": "backend", "status": "recycled", "generation_before": 1}],
                "fresh": [{"name": "backend-1", "kind": "backend", "generation": 2}],
                "recycle_stopped": None,
            },
        }
    )
    assert isinstance(body.recycle, RecycleRoll)
    assert body.recycle.recycle[0].status == "recycled"
    assert body.awaiting_respawn[0].name == "backend-2"


def test_an_apply_response_with_no_recycle_class_change_has_no_roll() -> None:
    from tai42_contract.app.responses import ApplyResponse

    body = ApplyResponse.model_validate({"status": "ok", "env_keys": 0, "fanout": {"mode": "local-only"}})
    assert body.recycle is None
    assert body.awaiting_respawn == []
    schema = ApplyResponse.model_json_schema()
    assert "recycle" not in schema["required"]
    assert "awaiting_respawn" not in schema["required"]


def test_a_backup_section_report_carries_the_recycle_roll() -> None:
    from tai42_contract.app.responses import RecycleRoll
    from tai42_contract.backup.models import BackupSectionReport

    report = BackupSectionReport.model_validate({"recycle": {"recycle": [], "fresh": [], "recycle_stopped": None}})
    assert isinstance(report.recycle, RecycleRoll)
    assert report.recycle is not None
    assert BackupSectionReport().recycle is None


def test_the_awaiting_respawn_verdict_names_workers_and_keys() -> None:
    from tai42_contract.app.responses import ApplyResponse, AwaitingWorker, ProfileApplyResponse

    body = ApplyResponse.model_validate(
        {
            "status": "ok",
            "env_keys": 1,
            "awaiting_respawn": [{"name": "serve-2", "keys": ["BACKEND_MANIFEST_KEY"]}],
            "fanout": {"mode": "fleet"},
        }
    )
    assert body.awaiting_respawn == [AwaitingWorker(name="serve-2", keys=["BACKEND_MANIFEST_KEY"])]
    profile = ProfileApplyResponse.model_validate(
        {"hot": [], "recycle": [], "fresh": [], "refused": [], "fanout": {"mode": "fleet"}}
    )
    assert profile.awaiting_respawn == []
    assert "awaiting_respawn" not in ProfileApplyResponse.model_json_schema()["required"]


def test_a_backup_section_report_carries_the_workers_awaiting_a_respawn() -> None:
    from tai42_contract.app.responses import AwaitingWorker
    from tai42_contract.backup.models import BackupSectionReport

    report = BackupSectionReport.model_validate(
        {"awaiting_respawn": [{"name": "serve-2", "keys": ["BACKEND_MANIFEST_KEY"]}]}
    )
    assert report.awaiting_respawn == [AwaitingWorker(name="serve-2", keys=["BACKEND_MANIFEST_KEY"])]
    assert BackupSectionReport().awaiting_respawn == []
    assert "awaiting_respawn" not in BackupSectionReport.model_json_schema().get("required", [])

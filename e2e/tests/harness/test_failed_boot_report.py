"""A stack whose boot failed has its evidence in the report of the test that booted it."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

from tai42_e2e import diagnostics
from tai42_e2e.stack import TaiStack

_FAKE_STACK = """
from types import SimpleNamespace
from tai42_e2e import diagnostics
from tai42_e2e.topology import Topology


def failed_boot():
    handle = SimpleNamespace(log_tail=lambda: "the process said why")
    config = SimpleNamespace(
        name="fresh", topology=Topology.MULTIWORKER, env={}, process_env={}, stored_env={}, manifest={}
    )
    stack = SimpleNamespace(config=config, app_ports=[1], metrics_port=None, metrics_dir="", procs={"serve": handle})
    diagnostics.record_failed_boot(diagnostics.stack_report(stack))
"""


def test_a_boot_that_raises_records_the_stack_and_raises_the_same_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError("the boot failed")
    recorded: list[object] = []

    def start() -> None:
        raise error

    monkeypatch.setattr(diagnostics, "stack_report", id)
    monkeypatch.setattr(diagnostics, "record_failed_boot", recorded.append)
    stack = SimpleNamespace(_start=start)
    with pytest.raises(RuntimeError) as raised:
        TaiStack.boot(cast(TaiStack, stack))
    assert raised.value is error
    assert recorded == [id(stack)]


def test_a_boot_the_test_timeout_stops_records_the_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[object] = []

    def start() -> None:
        pytest.fail("Timeout (>120.0s) from pytest-timeout.")

    monkeypatch.setattr(diagnostics, "stack_report", id)
    monkeypatch.setattr(diagnostics, "record_failed_boot", recorded.append)
    stack = SimpleNamespace(_start=start)
    with pytest.raises(pytest.fail.Exception):
        TaiStack.boot(cast(TaiStack, stack))
    assert recorded == [id(stack)]


def test_a_failed_boot_in_a_test_is_in_its_report(inner_pytest: Callable[..., str]) -> None:
    output = inner_pytest(_FAKE_STACK + "def test_boots():\n    failed_boot()\n    raise RuntimeError('boot')\n")
    assert "tai42-e2e failed boot diagnostics" in output, output
    assert "the process said why" in output, output


def test_a_failed_boot_in_a_fixture_is_in_the_report_of_the_test_it_set_up(inner_pytest: Callable[..., str]) -> None:
    test = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def booted():\n"
        "    failed_boot()\n"
        "    raise RuntimeError('boot')\n"
        "def test_boots(booted):\n"
        "    pass\n"
    )
    output = inner_pytest(_FAKE_STACK + test)
    assert "the process said why" in output, output


def test_a_failed_boot_the_test_expected_is_not_in_the_report_of_a_later_failure(
    inner_pytest: Callable[..., str],
) -> None:
    test = "def test_expects_it():\n    failed_boot()\ndef test_later():\n    raise RuntimeError('later')\n"
    output = inner_pytest(_FAKE_STACK + test)
    assert "1 failed, 1 passed" in output, output
    assert "the process said why" not in output, output


def test_a_failed_marketplace_boot_in_a_fixture_is_in_the_report_of_the_test_it_set_up(
    inner_pytest: Callable[..., str],
) -> None:
    test = (
        "from types import SimpleNamespace\n"
        "import pytest\n"
        "from tai42_e2e.marketplace import MarketplaceService\n"
        "class Service(MarketplaceService):\n"
        "    def __init__(self, root):\n"
        "        self.host, self.port, self.root = '127.0.0.1', 1, root\n"
        "        self._logs_dir = root / 'logs'\n"
        "        self._procs = {'mp-api': SimpleNamespace(log_tail=lambda: 'the registry said why')}\n"
        "    def _apply_ddl(self):\n"
        "        raise RuntimeError('boot')\n"
        "@pytest.fixture\n"
        "def service(tmp_path):\n"
        "    Service(tmp_path).boot()\n"
        "def test_boots(service):\n"
        "    pass\n"
    )
    output = inner_pytest(test)
    assert "tai42-e2e failed boot diagnostics" in output, output
    assert "=== service 'marketplace' on port 1 ===" in output, output
    assert "the registry said why" in output, output

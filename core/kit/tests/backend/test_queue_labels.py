"""Worker queue labels: the grammar, the queue each derives, and the worker CLIs' option callback."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from tai42_kit.backend import check_queue_label, derived_queue, queue_label_option


@pytest.mark.parametrize("label", ["gpu", "GPU_2", "a-b", "0"])
def test_a_label_of_letters_digits_underscore_or_dash_is_accepted(label: str) -> None:
    assert check_queue_label(label) == label


@pytest.mark.parametrize("label", ["", "a.b", "a:b", "a b", "gpu/1", "ü"])
def test_any_other_label_is_refused(label: str) -> None:
    with pytest.raises(ValueError, match=r"must match \[A-Za-z0-9_-\]\+"):
        check_queue_label(label)


def test_a_label_derives_its_queue_under_the_settings_queue() -> None:
    assert derived_queue("ns:default", "gpu") == "ns:default.gpu"


def test_no_label_is_the_settings_queue_itself() -> None:
    assert derived_queue("ns:default", None) == "ns:default"


def test_derivation_refuses_a_label_outside_the_grammar() -> None:
    with pytest.raises(ValueError, match=r"queue label 'a\.b'"):
        derived_queue("ns:default", "a.b")


@click.command()
@click.option("--queue", default=None, callback=queue_label_option)
def _worker(queue: str | None) -> None:
    click.echo(repr(queue))


def test_the_option_callback_passes_a_label_and_an_unset_option() -> None:
    runner = CliRunner()
    assert runner.invoke(_worker, ["--queue", "gpu"]).output == "'gpu'\n"
    assert runner.invoke(_worker, []).output == "None\n"


def test_the_option_callback_refuses_a_bad_label_as_a_bad_parameter() -> None:
    result = CliRunner().invoke(_worker, ["--queue", "a.b"])
    assert result.exit_code == 2
    assert "Invalid value for '--queue'" in result.output

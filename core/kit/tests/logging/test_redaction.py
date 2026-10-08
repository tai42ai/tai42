"""The kit's process log-redaction registries and URL redaction helpers.

Every redactor here is a neutral synthetic one (it masks ``SYNTH-<word>``), so the
registries are proven usable by any feature, not shaped after one.
"""

from __future__ import annotations

import logging
import re
import sys
import time

import pytest

from tai42_kit.logging import (
    REDACTOR_FAILED,
    TRANSPORT_LOGGERS,
    URL_REDACTION,
    install_record_redaction,
    redact_url_userinfo,
    redact_urls_in_text,
    redaction,
    register_record_redactor,
    register_transport_redaction,
)

_SYNTH_RE = re.compile(r"SYNTH-\w+")


def _mask_synth(text: str) -> str:
    return _SYNTH_RE.sub("SYNTH-<masked>", text)


def _raise(_text: str) -> str:
    raise RuntimeError("redactor exploded")


@pytest.fixture(autouse=True)
def _isolated_redaction_state(monkeypatch: pytest.MonkeyPatch):
    """Give every test the stock record factory, empty registries, the embed scope and no transport filter."""
    saved_factory = logging.getLogRecordFactory()

    def strip_transport_filters() -> None:
        for name in TRANSPORT_LOGGERS:
            target = logging.getLogger(name)
            for existing in [f for f in target.filters if isinstance(f, redaction._TransportRedactionFilter)]:
                target.removeFilter(existing)

    logging.setLogRecordFactory(logging.LogRecord)
    monkeypatch.setattr(redaction, "_SCOPE", "tai")
    monkeypatch.setattr(redaction, "_RECORD_REDACTORS", redaction._Registry())
    monkeypatch.setattr(redaction, "_TRANSPORT_REDACTORS", redaction._Registry())
    strip_transport_filters()
    try:
        yield
    finally:
        logging.setLogRecordFactory(saved_factory)
        strip_transport_filters()


def _make(name: str, msg: str, args: object = None, **kwargs: object) -> logging.LogRecord:
    """A record built through the live process record factory, as ``Logger.makeRecord`` does."""
    return logging.getLogRecordFactory()(name, logging.INFO, __file__, 1, msg, args, None, **kwargs)


def _register_synth() -> None:
    register_record_redactor("synthetic", markers=("SYNTH-",), redact=_mask_synth)


# --- record redaction -------------------------------------------------------------


def test_registration_alone_installs_nothing():
    _register_synth()
    assert logging.getLogRecordFactory() is logging.LogRecord
    assert _make("tai42_x", "SYNTH-secret").getMessage() == "SYNTH-secret"


def test_tai_scope_scrubs_msg_and_args_of_a_tai_family_logger():
    _register_synth()
    install_record_redaction()
    record = _make("tai42_x", "token %s seen", ("SYNTH-secret",))
    assert record.getMessage() == "token SYNTH-<masked> seen"
    # The record is rendered once and its args cleared, so no formatter re-interpolates the source.
    assert record.args is None


def test_tai_scope_scrubs_exc_text_and_stack_info():
    _register_synth()
    install_record_redaction()
    try:
        raise ValueError("SYNTH-in-exception")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.getLogRecordFactory()(
        "tai42_x", logging.ERROR, __file__, 1, "failed", None, exc_info, sinfo="Stack: SYNTH-instack"
    )
    assert record.exc_text is not None
    assert "SYNTH-in-exception" not in record.exc_text
    assert "SYNTH-<masked>" in record.exc_text
    assert record.stack_info == "Stack: SYNTH-<masked>"
    # The handler's formatter reuses the redacted exc_text verbatim.
    assert "SYNTH-in-exception" not in logging.Formatter().format(record)


def test_a_marker_free_exception_is_not_rendered():
    _register_synth()
    install_record_redaction()
    try:
        raise ValueError("nothing to hide")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.getLogRecordFactory()("tai42_x", logging.ERROR, __file__, 1, "failed", None, exc_info)
    assert record.exc_text is None


def test_tai_scope_leaves_a_host_logger_record_untouched():
    _register_synth()
    install_record_redaction()
    assert _make("hostapp", "SYNTH-secret").getMessage() == "SYNTH-secret"


@pytest.mark.parametrize(
    "name", ["tai", "mcp", "fastmcp", "tai42_kit", "tai42.child", "tai.embed", "mcp.shared.session", "fastmcp.x"]
)
def test_tai_scope_covers_the_tai_logger_family(name: str):
    _register_synth()
    install_record_redaction()
    assert _make(name, "SYNTH-secret").getMessage() == "SYNTH-<masked>"


@pytest.mark.parametrize("name", ["hostapp", "taix", "mcpserver", "tai42", "fastmcpish", "root"])
def test_tai_scope_excludes_other_loggers(name: str):
    _register_synth()
    install_record_redaction()
    assert _make(name, "SYNTH-secret").getMessage() == "SYNTH-secret"


def test_process_scope_scrubs_every_logger():
    _register_synth()
    install_record_redaction("process")
    assert _make("tai42_x", "SYNTH-a").getMessage() == "SYNTH-<masked>"
    assert _make("hostapp", "SYNTH-b").getMessage() == "SYNTH-<masked>"


def test_a_later_tai_install_never_narrows_the_process_scope():
    _register_synth()
    install_record_redaction()
    install_record_redaction("process")
    install_record_redaction("tai")
    assert _make("hostapp", "SYNTH-b").getMessage() == "SYNTH-<masked>"


def test_an_unknown_scope_is_refused():
    with pytest.raises(ValueError, match="scope must be one of"):
        install_record_redaction("everything")  # type: ignore[arg-type]


def test_installing_twice_stacks_nothing():
    calls: list[str] = []

    def counting(text: str) -> str:
        calls.append(text)
        return _mask_synth(text)

    register_record_redactor("synthetic", markers=("SYNTH-",), redact=counting)
    install_record_redaction()
    first = logging.getLogRecordFactory()
    install_record_redaction()
    assert logging.getLogRecordFactory() is first
    _make("tai42_x", "SYNTH-once")
    assert calls == ["SYNTH-once"]


def test_install_chains_the_prior_factory():
    seen: list[str] = []

    def prior(*args: object, **kwargs: object) -> logging.LogRecord:
        record = logging.LogRecord(*args, **kwargs)  # type: ignore[arg-type]
        seen.append(record.name)
        return record

    logging.setLogRecordFactory(prior)
    _register_synth()
    install_record_redaction()
    assert _make("tai42_x", "SYNTH-a").getMessage() == "SYNTH-<masked>"
    assert seen == ["tai42_x"]


def test_registering_by_the_same_name_replaces():
    _register_synth()
    register_record_redactor("synthetic", markers=("SYNTH-",), redact=lambda text: "replaced")
    install_record_redaction()
    assert _make("tai42_x", "SYNTH-a").getMessage() == "replaced"


def test_every_matching_redactor_applies_and_a_marker_free_one_does_not():
    _register_synth()
    register_record_redactor("other", markers=("OTHER-",), redact=lambda text: text.replace("OTHER-x", "OTHER-*"))
    register_record_redactor("never", markers=("ABSENT",), redact=_raise)
    install_record_redaction()
    assert _make("tai42_x", "SYNTH-a and OTHER-x").getMessage() == "SYNTH-<masked> and OTHER-*"


def test_a_marker_free_record_keeps_its_lazy_args():
    _register_synth()
    install_record_redaction()
    record = _make("tai42_x", "value %s", ("plain",))
    assert record.args == ("plain",)


def test_a_raising_record_redactor_fails_closed():
    register_record_redactor("broken", markers=("SYNTH-",), redact=_raise)
    install_record_redaction()
    record = _make("tai42_x", "SYNTH-%s", ("secret",), sinfo="stack")
    assert record.msg == REDACTOR_FAILED
    assert record.args is None
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None


def test_a_raising_repr_during_detection_fails_closed():
    class _Boom:
        def __repr__(self) -> str:
            raise RuntimeError("repr exploded")

    _register_synth()
    install_record_redaction()
    record = _make("tai42_x", "value %r", (_Boom(),))
    assert record.msg == REDACTOR_FAILED
    assert record.args is None


@pytest.mark.parametrize("marker", [None, ""])
def test_a_marker_that_is_not_a_non_empty_string_is_refused(marker: object):
    expected = f"record redactor 'bad': every marker must be a non-empty string, got {marker!r}"
    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        register_record_redactor("bad", markers=("SYNTH-", marker), redact=_mask_synth)  # type: ignore[arg-type]


def test_a_record_redactor_without_markers_is_refused():
    with pytest.raises(ValueError, match="record redactor 'bad': at least one marker is required"):
        register_record_redactor("bad", markers=(), redact=_mask_synth)


# --- transport redaction ----------------------------------------------------------


def test_transport_redaction_masks_both_transport_loggers(caplog: pytest.LogCaptureFixture):
    register_transport_redaction("synthetic", markers=("SYNTH-",), redact=_mask_synth)
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET", "https://h/SYNTH-a", "OK")
        logging.getLogger("httpcore").info("connect url=%s", "https://h/SYNTH-b")
    assert "SYNTH-a" not in caplog.text
    assert "SYNTH-b" not in caplog.text
    assert 'HTTP Request: GET https://h/SYNTH-<masked> "OK"' in caplog.text
    assert all(record.args is None for record in caplog.records)


def test_transport_redaction_leaves_other_loggers_and_clean_records(caplog: pytest.LogCaptureFixture):
    register_transport_redaction("synthetic", markers=("SYNTH-",), redact=_mask_synth)
    with caplog.at_level(logging.INFO):
        logging.getLogger("hostapp").info("SYNTH-host")
        logging.getLogger("httpx").info("plain %s", "value")
    assert "SYNTH-host" in caplog.text
    assert caplog.records[1].args == ("value",)


def test_transport_install_is_idempotent():
    register_transport_redaction("synthetic", markers=("SYNTH-",), redact=_mask_synth)
    register_transport_redaction("other", markers=("OTHER-",), redact=lambda text: text)
    for name in TRANSPORT_LOGGERS:
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, redaction._TransportRedactionFilter)]
        assert len(filters) == 1


def test_a_raising_transport_redactor_fails_closed(caplog: pytest.LogCaptureFixture):
    register_transport_redaction("broken", markers=("SYNTH-",), redact=_raise)
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("url=%s", "https://h/SYNTH-a")
    assert "SYNTH-a" not in caplog.text
    assert caplog.records[0].getMessage() == REDACTOR_FAILED


def test_a_transport_marker_that_is_not_a_non_empty_string_is_refused():
    expected = "transport redactor 'bad': every marker must be a non-empty string, got ''"
    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        register_transport_redaction("bad", markers=("",), redact=_mask_synth)


# --- URL redaction ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://u:p@h/x", f"https://{URL_REDACTION}@h/x"),
        ("redis://user:pw@host:6379/0", f"redis://{URL_REDACTION}@host:6379/0"),
        ("redis://:pw@host:6379/0", f"redis://{URL_REDACTION}@host:6379/0"),
        ("https://u:p@ss@h/x?y=1", f"https://{URL_REDACTION}@h/x?y=1"),
        ("https://u@h", f"https://{URL_REDACTION}@h"),
        ("https://u:p@h?q=1", f"https://{URL_REDACTION}@h?q=1"),
        ("https://u:p@h#frag", f"https://{URL_REDACTION}@h#frag"),
        ("https://h?e=a@b", "https://h?e=a@b"),
        ("https://h/p@q", "https://h/p@q"),
        ("https://h#a@b", "https://h#a@b"),
        ("https://h/x", "https://h/x"),
        ("u:p@h/x", "u:p@h/x"),
        ("", ""),
    ],
)
def test_redact_url_userinfo(url: str, expected: str):
    assert redact_url_userinfo(url) == expected


def test_redact_urls_in_text_masks_userinfo_and_query_values():
    text = "probe failed: GET https://u:p@h/x?token=abc&flag&k=v#frag then retried http://h2/y"
    assert redact_urls_in_text(text) == (
        f"probe failed: GET https://{URL_REDACTION}@h/x?token={URL_REDACTION}&flag&k={URL_REDACTION}#frag "
        "then retried http://h2/y"
    )


def test_redact_urls_in_text_keeps_the_last_at_of_the_authority():
    assert redact_urls_in_text("see https://u:p@ss@h/x") == f"see https://{URL_REDACTION}@h/x"


def test_redact_urls_in_text_leaves_non_url_text_untouched():
    text = "user@example plain words key=value ?q=1"
    assert redact_urls_in_text(text) == text


def test_redact_urls_in_text_scans_an_adversarial_scheme_run_linearly():
    # A long run of scheme-valid characters never followed by "://" must not backtrack quadratically.
    text = "a" * 200_000
    started = time.perf_counter()
    assert redact_urls_in_text(text) == text
    assert time.perf_counter() - started < 2.0

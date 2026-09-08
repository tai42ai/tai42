"""Unit behavior of ``access_control.path_canon.canonicalize_path``.

The canonical form is the RAW request target split on ``/``, each segment decoded
ONCE (the single decode the ASGI router / ``RawPathRoute`` applies) then re-encoded for
only ``/`` → ``%2F`` and ``%`` → ``%25``. So a data slash inside a segment stays inside
ONE segment, a double-encoded slash stays distinct from a single one, and the function is
idempotent — canonicalizing a canonical path returns it unchanged.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.access_control.path_canon import (
    MalformedPathError,
    canonicalize_path,
    request_canonical_path,
    under_prefix,
)

# -- single decode, reversible re-encode -------------------------------------


def test_plain_path_is_identity():
    assert canonicalize_path("/agents/inner/view") == "/agents/inner/view"


def test_encoded_slash_stays_one_segment():
    # ``%2F`` decodes to a data ``/`` that is re-encoded, so the key stays ONE segment
    # (never split into two) — the same form ``RawPathRoute`` keeps for the record doors.
    assert canonicalize_path("/api/states/s/records/agent/a/thread/x%2Fy") == (
        "/api/states/s/records/agent/a/thread/x%2Fy"
    )
    # Lower-case hex is the same slash, normalized to the canonical upper-case escape.
    assert canonicalize_path("/x/a%2fb") == "/x/a%2Fb"


def test_double_encoded_slash_stays_distinct_from_a_single_one():
    # ``%252F`` decodes once to the literal text ``%2F`` (a thread key whose principal
    # itself carried ``/``); it re-encodes to ``%252F`` — distinct from a single ``%2F``.
    assert canonicalize_path("/x/a%252Fb") == "/x/a%252Fb"
    assert canonicalize_path("/x/a%252Fb") != canonicalize_path("/x/a%2Fb")


def test_bare_percent_is_re_encoded_reversibly():
    # A ``%`` (a lone one or an invalid escape) decodes to itself and re-encodes to
    # ``%25`` so the form is reversible and never mistaken for an escape on a second pass.
    assert canonicalize_path("/segment/50%") == "/segment/50%25"
    assert canonicalize_path("/x/%zz") == "/x/%25zz"


def test_canonicalize_is_idempotent():
    for raw in ("/x/a%2Fb", "/x/a%252Fb", "/segment/50%", "/api/../agents"):
        once = canonicalize_path(raw)
        assert canonicalize_path(once) == once


# -- NUL / control / backslash rejection (after the single decode) -----------


@pytest.mark.parametrize(
    "path",
    [
        "/agents\x00",
        "/agents\x1f",
        "/agents\x7f",
        "/agents\\admin",
        "/x/a%00b",  # a decoded NUL
        "/x%5Cy",  # a decoded backslash
    ],
)
def test_control_and_backslash_rejected(path):
    with pytest.raises(MalformedPathError, match="NUL, control, or backslash"):
        canonicalize_path(path)


# -- slash collapse + dot-segment resolution (per decoded segment) -----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//api/x", "/api/x"),  # duplicate leading slash collapses
        ("/a//b///c", "/a/b/c"),  # interior duplicate slashes collapse
        ("/a/b/", "/a/b"),  # trailing slash dropped
        ("/", "/"),  # root preserved
        ("/a/./b", "/a/b"),  # current-dir segment removed
        ("/api/../agents", "/agents"),  # dot-resolution before any prefix check
        ("/agents/../api/secret", "/api/secret"),  # …and the reverse
        ("/../../etc", "/etc"),  # a ".." that escapes root normalizes to root
        ("/a/%2E%2E/b", "/b"),  # an ENCODED ".." resolves per decoded segment too
    ],
)
def test_slash_and_dot_normalization(raw, expected):
    assert canonicalize_path(raw) == expected


# -- request_canonical_path from the raw scope target ------------------------


def test_request_canonical_path_reads_the_raw_target():
    scope = {"raw_path": b"/api/states/s/records/agent/a/thread/x%2Fy", "path": "/decoded/ignored"}
    assert request_canonical_path(scope) == "/api/states/s/records/agent/a/thread/x%2Fy"


def test_request_canonical_path_falls_back_to_decoded_path_when_no_raw():
    assert request_canonical_path({"path": "/api/states/s"}) == "/api/states/s"


def test_request_canonical_path_strips_the_mounted_root_path_like_the_router():
    scope = {
        "raw_path": b"/mount/api/states/s/records/agent/a/thread/x%2Fy",
        "path": "/mount/api/states/s/records/agent/a/thread/x/y",
        "root_path": "/mount",
    }
    assert request_canonical_path(scope) == "/api/states/s/records/agent/a/thread/x%2Fy"
    assert request_canonical_path({"path": "/mount/api/states/s", "root_path": "/mount"}) == "/api/states/s"
    # A root_path that is not a whole-segment prefix is not stripped.
    assert request_canonical_path({"raw_path": b"/mountain/api", "root_path": "/mount"}) == "/mountain/api"


def test_request_canonical_path_rejects_non_ascii_raw_target():
    with pytest.raises(MalformedPathError):
        request_canonical_path({"raw_path": "/x/café".encode(), "path": "/x"})


# -- segment-aware prefix helper ---------------------------------------------


def test_under_prefix_is_segment_aware():
    assert under_prefix("/api", "/api") is True
    assert under_prefix("/api/x", "/api") is True
    assert under_prefix("/apiary", "/api") is False

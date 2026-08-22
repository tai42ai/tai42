"""Harness self-test for the browser-e2e studio stack's access-control route map.

The studio stack resolves a request path in two tiers: ``STUDIO_PATH_PATTERNS``
(``ACCESS_CONTROL_PATH_PATTERNS``) fullmatches the path to a route TEMPLATE, then the
tier-two route table ``seed_studio_auth`` seeds maps that template to a resource id.
This pins that resolution for the interactions doors the way the real verifier runs it
(the tier-1 fullmatch union, then the tier-2 template lookup), so an auth-flip is caught
here without booting a stack: the served-media capability url (a tokenless ``<img>``
fetch) resolves PUBLIC exactly as the callback door does, while the authed studio
surface (the interactions stream) stays protected.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from tai42_skeleton.access_control.settings import AccessControlSettings

from tai42_e2e import harness

pytestmark = pytest.mark.backendless

# A well-formed served-media reference: the fixed route prefix + a 43-char urlsafe id.
_MEDIA_PATH = "/api/interactions/media/" + "a" * 43
_CALLBACK_PATH = "/api/interactions/callback/some-ticket"
_STREAM_PATH = "/api/interactions/stream"


def _tier_two(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The ``{template: resource_id}`` map ``seed_studio_auth`` writes into the route
    store, captured without a backend by intercepting the two seed helpers it forwards
    ``infra``/``resources`` to."""
    captured: dict[str, str] = {}

    def _capture_rows(resources: Any, rows: Any) -> None:
        for url, scope_id, _pattern in rows:
            captured[url] = scope_id

    # ``infra``/``resources`` are forwarded only to the two stubbed seed helpers, so the
    # seed writes nothing and the arguments are never dereferenced.
    monkeypatch.setattr(harness, "seed_root_identity", lambda *a, **k: "sk-test")
    monkeypatch.setattr(harness, "seed_route_rows", _capture_rows)
    harness.seed_studio_auth(cast(Any, None), cast(Any, None), api_key="sk-test")
    return captured


def _resolve(settings: AccessControlSettings, tier_two: dict[str, str], path: str) -> set[str]:
    """The resource ids a path resolves to under the studio map: the verifier's tier-1
    fullmatch union of templates, each looked up in the tier-2 table (deny-wins union)."""
    templates = {template for pattern, template in settings.compiled_patterns if pattern.fullmatch(path)}
    return {tier_two[template] for template in templates if template in tier_two}


def test_media_door_resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = AccessControlSettings(path_patterns=harness.STUDIO_PATH_PATTERNS)
    tier_two = _tier_two(monkeypatch)

    # The served-media door is a public capability url: a browser <img> loads it with no
    # credential, so it must NOT fall into the authed catch-all.
    assert _resolve(settings, tier_two, _MEDIA_PATH) == {settings.public_resource_id}
    assert harness.STUDIO_RESOURCE_ID not in _resolve(settings, tier_two, _MEDIA_PATH)


def test_callback_door_still_public(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = AccessControlSettings(path_patterns=harness.STUDIO_PATH_PATTERNS)
    tier_two = _tier_two(monkeypatch)

    assert _resolve(settings, tier_two, _CALLBACK_PATH) == {settings.public_resource_id}


def test_stream_stays_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = AccessControlSettings(path_patterns=harness.STUDIO_PATH_PATTERNS)
    tier_two = _tier_two(monkeypatch)

    # The authed studio surface resolves to the single protected resource, never public.
    assert _resolve(settings, tier_two, _STREAM_PATH) == {harness.STUDIO_RESOURCE_ID}

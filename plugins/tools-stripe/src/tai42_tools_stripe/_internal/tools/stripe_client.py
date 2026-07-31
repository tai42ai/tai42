"""Direct Stripe REST client and interaction-callback bridge for the Stripe payment tools.

Speaks Stripe's REST API through tai42-kit's curl client (no ``stripe`` SDK): creates and lists
Checkout Sessions, asserts a session's ``livemode`` against the configured key's mode, builds the
whitelisted answer, and POSTs it to the interaction callback door under an exact-origin SSRF pin
with a bounded transient retry.

Facts pinned here are read from Stripe's published docs (https://docs.stripe.com) and nowhere else:

- GA API version ``2026-06-24.dahlia`` (docs.stripe.com/api/versioning), sent as ``Stripe-Version``.
- The create body is form-encoded with bracket notation
  ``line_items[0][price_data][product_data][name]`` and ``metadata[<key>]``
  (docs.stripe.com/api/checkout/sessions/create); ``success_url`` is optional at Stripe.
- The list call filters on ``created[gte]`` and ``status``, pages with the ``starting_after``
  cursor, and caps ``limit`` at 100 (docs.stripe.com/api/checkout/sessions/list).
- ``payment_status`` is one of ``paid`` / ``unpaid`` / ``no_payment_required`` and ``amount_total``
  is the total after tax and discounts (docs.stripe.com/api/checkout/sessions/object).
- Secret keys are ``sk_live_`` / ``sk_test_`` and restricted keys ``rk_live_`` / ``rk_test_``
  (docs.stripe.com/keys); metadata allows 50 keys of up to 40 chars with 500-char values
  (docs.stripe.com/api/metadata).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from typing import Any, cast
from urllib.parse import urlencode, urlparse

from curl_cffi import CurlError
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.curl import CurlClient
from tai42_kit.settings import TaiBaseSettings, settings_cache

_logger = logging.getLogger(__name__)

# The Stripe REST API version every request in this module pins. Direct REST carries no
# SDK-pinned version, and an unpinned request is served the ACCOUNT's dashboard-selected
# version -- so the response shape would vary per deployment and could change without a
# release. Read from Stripe's published API-version docs (docs.stripe.com/api/versioning).
STRIPE_API_VERSION = "2026-06-24.dahlia"

# The longest single wait a door-supplied Retry-After may buy. The callback door's limiter is a
# FIXED-WINDOW counter and its Retry-After is the seconds left in that window -- at most 60 for the
# per-minute window, at most 10 for the 10-second burst window. Waiting the window out is the only
# thing that clears it, so a value shorter than the door asked for retries into a window that is
# still closed and burns the attempt for nothing. 90 clears the door's own maximum with headroom
# while still bounding a hostile or absurd header: beyond it the computed backoff applies instead.
RETRY_AFTER_MAX_SECONDS = 90

# The bounded transient-retry ladder for a callback-door POST: 5 attempts total, base delay 0.5s,
# factor 2, with a small positive jitter fraction. The four computed delays are ~0.5s, 1s, 2s, 4s;
# their sum (~7.5s) is the ladder's budget, a description of the computed ladder rather than a
# ceiling on waiting -- an honoured Retry-After deliberately exceeds it.
_RETRY_ATTEMPTS = 5
_RETRY_BASE_DELAY = 0.5
_RETRY_FACTOR = 2
_RETRY_JITTER_FRACTION = 0.1

# Stripe's list-endpoint per-page maximum, and the hard page ceiling the reconciler lists under.
# An unchecked page loop is not a bound at all: a truncated reconciliation run would report a clean
# summary while leaving paid sessions unanswered, exactly the silent failure the design forbids.
_LIST_LIMIT = 100
_LIST_PAGE_CEILING = 50

# Indirection points so tests can observe the backoff without waiting and make the jitter
# deterministic; nothing else reads them.
_sleep = asyncio.sleep
_rng = random.Random()


class StripeLivemodeMismatch(Exception):
    """A Checkout Session's ``livemode`` disagrees with the configured key's mode.

    Its own type because a cross-mode session is not one session's bad luck -- it means the
    process holds the wrong-mode key -- so a batch caller collects it into ``failed`` and raises
    rather than treating it as a per-session verdict.
    """


class CallbackTargetRefused(Exception):
    """A callback URL failed the exact-origin SSRF pin.

    Its own type because a refusal means the callback URLs the platform mints disagree with this
    process's ``INTERACTIONS_PUBLIC_BASE_URL`` -- true of every session -- so a batch caller
    collects it into ``failed`` and raises.
    """


class CallbackDoorError(Exception):
    """A non-2xx verdict from the interaction callback door, carrying the door's status and body.

    ``__str__`` is the fixed, status-bearing literal a downstream log grep matches on; the
    reconciler branches on ``.status`` (404 -> expired, 400 -> rejected, else -> failed) rather
    than reading the message.
    """

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"callback door refused the answer: HTTP {status} {body}")


class StripeSettings(TaiBaseSettings):
    """Stripe payment-tool configuration, read from ``STRIPE_``-prefixed env plus two unprefixed
    platform names that the tools share with the rest of the deployment."""

    model_config = SettingsConfigDict(env_prefix="STRIPE_")

    secret_key: SecretStr | None = None
    api_base: str = "https://api.stripe.com"
    request_timeout_seconds: float = 20
    reconcile_answer_interval_seconds: float = Field(default=1.2, ge=0)
    bridge_callback_secret: SecretStr | None = Field(default=None, validation_alias="TAI_BRIDGE_CALLBACK_SECRET")
    interactions_public_base_url: str | None = Field(default=None, validation_alias="INTERACTIONS_PUBLIC_BASE_URL")


@settings_cache
def stripe_settings() -> StripeSettings:
    return StripeSettings()


def _secret_key() -> str:
    """The configured Stripe secret key, revealed.

    Missing and EMPTY are the same failure and both raise naming ``STRIPE_SECRET_KEY``: an empty
    env var parses to ``SecretStr("")`` (not ``None``), so a plain ``is None`` test would send
    ``Authorization: Bearer`` to Stripe. This is the module's only read path for the value, so the
    error is identical on every path -- including the one that never talks to Stripe.
    """
    key = stripe_settings().secret_key
    if not (key and key.get_secret_value()):
        raise ValueError("STRIPE_SECRET_KEY is not set (missing or empty)")
    return key.get_secret_value()


def _expected_livemode() -> bool:
    """The livemode the configured key implies: ``True`` for ``sk_live_``/``rk_live_``, ``False``
    for ``sk_test_``/``rk_test_``. Restricted keys are recognised deliberately -- they are
    Stripe's recommended integration credential. Any other prefix raises rather than guessing a
    mode; the key value is never echoed, so no secret can reach a log.
    """
    key = _secret_key()
    if key.startswith(("sk_live_", "rk_live_")):
        return True
    if key.startswith(("sk_test_", "rk_test_")):
        return False
    raise ValueError(
        "STRIPE_SECRET_KEY has an unrecognised key prefix; expected one of sk_live_, sk_test_, rk_live_, rk_test_"
    )


def _assert_livemode(session: dict[str, Any]) -> None:
    """Raise ``StripeLivemodeMismatch`` unless the session's ``livemode`` matches the key's mode.

    ``livemode`` is a required boolean: a missing or non-bool value raises loudly.
    """
    expected = _expected_livemode()
    actual = session.get("livemode")
    if not isinstance(actual, bool):
        raise ValueError(f"session 'livemode' is missing or not a boolean: {actual!r}")
    if actual != expected:
        raise StripeLivemodeMismatch(f"session livemode is {actual} but the configured key expects livemode {expected}")


async def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    data: str | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    """Issue one request through a fresh curl session and return ``(status, lowercased headers,
    body text)``. Redirects are OFF: the callback POST carries ``X-TAI-Bridge-Secret`` and both
    Stripe calls carry ``Authorization: Bearer <key>``, and libcurl replays custom headers across a
    redirect hop -- a 302 off a validated origin would hand those headers to another host.
    """
    session_ctx = tai42_app.clients.client_ctx(CurlClient, session_params={}, fresh=True)
    async with session_ctx as session:
        resp = await session.request(
            url=url,
            method=cast("Any", method),
            headers=headers,
            data=data,
            params=params,
            timeout=stripe_settings().request_timeout_seconds,
            allow_redirects=False,
        )
        resp_headers = {key.lower(): value for key, value in resp.headers.items() if value is not None}
        return resp.status_code, resp_headers, resp.text


async def create_checkout_session(
    *,
    amount: int,
    currency: str,
    product_name: str,
    success_url: str,
    cancel_url: str | None,
    metadata: dict[str, str],
    idempotency_basis: str,
) -> dict[str, Any]:
    """POST ``/v1/checkout/sessions`` for a one-line ``mode=payment`` session and return the
    response JSON whole. A non-2xx raises loudly with Stripe's status and message (never a silent
    default), and no request-header content is ever echoed into the raised text.

    The ``Idempotency-Key`` is deterministic -- ``"tai-" + sha256(idempotency_basis)`` -- so a
    retried create for the SAME logical request (identical ``idempotency_basis``) returns Stripe's
    stored original session instead of minting a second. The caller supplies a basis that uniquely
    identifies the request (a per-ask callback URL, or a canonical hash of the request's arguments).
    """
    pairs: list[tuple[str, str]] = [
        ("mode", "payment"),
        ("success_url", success_url),
        ("line_items[0][price_data][currency]", currency),
        ("line_items[0][price_data][unit_amount]", str(amount)),
        ("line_items[0][price_data][product_data][name]", product_name),
        ("line_items[0][quantity]", "1"),
    ]
    if cancel_url is not None:
        pairs.append(("cancel_url", cancel_url))
    for key, value in metadata.items():
        pairs.append((f"metadata[{key}]", value))

    idempotency_key = "tai-" + hashlib.sha256(idempotency_basis.encode()).hexdigest()
    headers = {
        "Authorization": f"Bearer {_secret_key()}",
        "Stripe-Version": STRIPE_API_VERSION,
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    url = f"{stripe_settings().api_base}/v1/checkout/sessions"
    status, _resp_headers, text = await _http_request("POST", url, headers=headers, data=urlencode(pairs))
    if not 200 <= status < 300:
        raise ValueError(f"Stripe checkout session create failed: HTTP {status} {text}")
    return json.loads(text)


async def list_checkout_sessions(created_gte: int) -> list[dict[str, Any]]:
    """GET all completed Checkout Sessions created at or after ``created_gte`` (unix seconds),
    following Stripe's ``starting_after`` cursor.

    ``status=complete`` narrows server-side (only a completed session can be paid); the caller
    still tests ``payment_status == "paid"`` because they are different fields. The loop terminates
    on ``has_more == false`` and on an empty page, and RAISES at a hard ceiling of
    ``_LIST_PAGE_CEILING`` pages -- naming the ceiling and the ``created_gte`` it was listing from,
    which is all this function holds -- rather than truncating a reconciliation run silently.
    """
    url = f"{stripe_settings().api_base}/v1/checkout/sessions"
    headers = {
        "Authorization": f"Bearer {_secret_key()}",
        "Stripe-Version": STRIPE_API_VERSION,
    }
    sessions: list[dict[str, Any]] = []
    starting_after: str | None = None
    pages = 0
    while True:
        if pages >= _LIST_PAGE_CEILING:
            raise ValueError(
                f"Stripe checkout session list exceeded its {_LIST_PAGE_CEILING}-page ceiling "
                f"({_LIST_PAGE_CEILING * _LIST_LIMIT} sessions) listing created[gte]={created_gte}"
            )
        params = {"created[gte]": str(created_gte), "status": "complete", "limit": str(_LIST_LIMIT)}
        if starting_after is not None:
            params["starting_after"] = starting_after
        status, _resp_headers, text = await _http_request("GET", url, headers=headers, params=params)
        if not 200 <= status < 300:
            raise ValueError(f"Stripe checkout session list failed: HTTP {status} {text}")
        payload = json.loads(text)
        pages += 1
        page = payload.get("data") or []
        if not page:
            break
        sessions.extend(page)
        if not payload.get("has_more"):
            break
        starting_after = page[-1]["id"]
    return sessions


def build_answer_payload(session: dict[str, Any]) -> dict[str, Any]:
    """The ONE place the whitelisted answer is built, so the bridge and the reconciler can never
    answer with different shapes. ``metadata`` is deliberately absent -- it holds the ask's own
    ticket. Required fields are read directly (a missing one raises); ``customer_email`` is the
    only known-optional field and falls back from the flattened key to ``customer_details.email``.
    """
    email = session.get("customer_email")
    if email is None:
        email = (session.get("customer_details") or {}).get("email")
    return {
        "status": "paid",
        "session_id": session["id"],
        "payment_intent": session["payment_intent"],
        "amount_total": session["amount_total"],
        "currency": session["currency"],
        "customer_email": email,
    }


def _effective_port(parsed: Any) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _assert_callback_target(callback_url: str) -> None:
    """Pin the callback POST target to the platform's own callback door by EXACT origin match.

    ``urlparse`` both sides and require equal scheme, hostname (parsed and lowercased, so userinfo
    and port cannot spoof it) and effective port, plus a path prefix DERIVED from the base's own
    path so a deployment mounted under a subpath still validates. Any ``..`` segment is rejected
    outright -- curl normalizes it before dialing, so a prefix check alone is escapable. Anything
    else raises ``CallbackTargetRefused``. The URL is used verbatim; the pin validates, it does
    not reconstruct.
    """
    base = stripe_settings().interactions_public_base_url
    if not base:
        raise ValueError("INTERACTIONS_PUBLIC_BASE_URL is not set (missing or empty)")
    target = urlparse(callback_url)
    origin = urlparse(base)
    required_prefix = origin.path.rstrip("/") + "/api/interactions/callback/"
    if ".." in target.path.split("/"):
        raise CallbackTargetRefused(f"callback URL path contains a '..' segment: {callback_url!r}")
    if not (
        target.scheme == origin.scheme
        and (target.hostname or "").lower() == (origin.hostname or "").lower()
        and _effective_port(target) == _effective_port(origin)
        and target.path.startswith(required_prefix)
    ):
        raise CallbackTargetRefused(f"callback URL {callback_url!r} is not on the pinned origin {base!r}")


def _computed_backoff(attempt: int) -> float:
    base = _RETRY_BASE_DELAY * (_RETRY_FACTOR**attempt)
    return base + _rng.random() * base * _RETRY_JITTER_FRACTION


def _parse_retry_after(value: str) -> float | None:
    """Parse a ``Retry-After`` header as delta-seconds. Returns the value only when it is a
    non-negative integer at or under ``RETRY_AFTER_MAX_SECONDS``; a larger, negative, non-integer,
    or HTTP-date value returns ``None`` so the caller falls back to the computed backoff.
    """
    try:
        seconds = int(value)
    except ValueError:
        return None
    if seconds < 0 or seconds > RETRY_AFTER_MAX_SECONDS:
        return None
    return float(seconds)


def _transient_delay(status: int, resp_headers: dict[str, str], attempt: int) -> float:
    """The wait before the next attempt for a transient response. A 429's ``Retry-After`` is
    honoured IN FULL when usable (a fixed-window limiter only clears by being waited out);
    otherwise the computed backoff applies, and an unusable header is logged, never silently
    ignored and never a raise.
    """
    if status == 429:
        raw = resp_headers.get("retry-after")
        if raw is not None:
            honoured = _parse_retry_after(raw)
            if honoured is not None:
                return honoured
            _logger.warning(
                "callback door Retry-After %r is out of range or unparsable; using computed backoff",
                raw,
            )
    return _computed_backoff(attempt)


def _is_transient(status: int) -> bool:
    # 5xx is the door failing; 408 is a read timeout (says nothing about the payload); 429 is a
    # rate limiter's "come back later". Everything else 4xx is a verdict and fails fast.
    return status >= 500 or status in (408, 429)


async def post_answer(callback_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST the answer JSON to the callback door with the ``X-TAI-Bridge-Secret`` header, under
    the exact-origin SSRF pin and a bounded transient retry.

    Missing and EMPTY ``TAI_BRIDGE_CALLBACK_SECRET`` are the same failure and both raise naming it:
    an empty secret would authenticate against a door whose verifier accepts the empty string.
    Transient failures (connection errors, 5xx, 408, 429) retry with exponential backoff; a 4xx
    verdict (400/401/403/404 and every other named 4xx) raises ``CallbackDoorError`` on the first
    response. After the attempt cap, RAISES naming the count and the last status/error -- never a
    swallowed failure or a ``None`` return.
    """
    _assert_callback_target(callback_url)
    secret = stripe_settings().bridge_callback_secret
    if not (secret and secret.get_secret_value()):
        raise ValueError("TAI_BRIDGE_CALLBACK_SECRET is not set (missing or empty)")
    headers = {
        "X-TAI-Bridge-Secret": secret.get_secret_value(),
        "Content-Type": "application/json",
    }
    body = json.dumps(payload)

    last_status: int | None = None
    last_error: BaseException | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            status, resp_headers, text = await _http_request("POST", callback_url, headers=headers, data=body)
        except CurlError as exc:
            last_status, last_error = None, exc
            if attempt == _RETRY_ATTEMPTS - 1:
                break
            await _sleep(_computed_backoff(attempt))
            continue
        if 200 <= status < 300:
            return json.loads(text)
        if not _is_transient(status):
            raise CallbackDoorError(status, text)
        last_status, last_error = status, None
        if attempt == _RETRY_ATTEMPTS - 1:
            break
        await _sleep(_transient_delay(status, resp_headers, attempt))
    raise ValueError(
        f"callback door answer failed after {_RETRY_ATTEMPTS} attempts; "
        f"last status={last_status} last error={last_error!r}"
    )

"""The channel stack profile and its per-medium env."""

from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _switch
from tai42_e2e.manifests.tool_entries import _INTERACTIONS_ENTRY, _PROJECTED_API_TOOLS
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The channel profile's per-medium recipient policy. In every medium the default
# recipient is deliberately absent from the allowlist: the allowlist gates only
# caller-supplied recipients, never the operator default, so a default-recipient send
# passing is itself the trusted-default proof.
TELEGRAM_DEFAULT_RECIPIENT = "910001"


TELEGRAM_ALLOWED_RECIPIENTS = ("910002", "910003")


TELEGRAM_UNLISTED_RECIPIENT = "990009"


SLACK_DEFAULT_RECIPIENT = "C0DEFAULT0"


SLACK_ALLOWED_RECIPIENTS = ("C0ALLOWED1", "C0ALLOWED2")


SLACK_UNLISTED_RECIPIENT = "C0UNLISTED"


# The app's own bot user id (``U…``): the bridge's ``our_identity`` and the self-message
# filter. Required at boot by the slack channel; distinct from any inbound event ``user``
# the stubbed Events API replies carry (they set none), so no test inbound is self-filtered.
SLACK_BOT_USER_ID = "U0BOTUSER0"


TWILIO_FROM = "+15559999999"


TWILIO_DEFAULT_RECIPIENT = "+15550000100"


TWILIO_ALLOWED_RECIPIENTS = ("+15550000200", "+15550000300")


TWILIO_UNLISTED_RECIPIENT = "+15559990009"


# The web channel carries NO recipient policy of its own: it has no operator default and
# no allowlist, because a web "recipient" is not an operator-chosen address but the
# visitor's own session — a delivery names ``"<web route identity>:<visitor session id>"``
# and the cookie holding that id is the only credential that can read or answer it.
WEB_IDENTITY = "e2e-web-site"


# Concurrent SSE streams one visitor may hold on a web stack, pinned into the profile env
# so the stream-cap leg drives a number the harness owns rather than the plugin's default.
WEB_MAX_STREAMS_PER_VISITOR = 4


# How often ONE web question's record may be put back after a refused or failed answer
# forward. No leg drives a refused forward, so this only has to stay clear of an accidental
# re-answer; pinned rather than inherited so the number the suite runs on is the harness's.
WEB_MAX_ANSWER_RESTORES = 50


# Bytes read from a web POST body before the door refuses with 413, pinned for the same
# reason: the body-cap leg reads this back instead of restating the plugin's default.
WEB_MAX_BODY_BYTES = 65536


# The channel seams whose real leg swaps the recording stub for the live vendor:
# each drops its ``CHANNEL_<X>_API_BASE_URL`` (the plugin default is the real vendor
# host) and reads its bot credential + test recipient from the operator template.
_CHANNEL_SEAMS = ("telegram", "slack", "twilio")


def _telegram_channel_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_CHAT_ID -> default + sole allowlisted recipient. No
        # API_BASE_URL (plugin default = real api.telegram.org); the webhook secret
        # is harness-minted (Telegram echoes whatever we register with setWebhook).
        chat = os.environ["CHANNEL_TELEGRAM_TEST_CHAT_ID"]
        return {
            "CHANNEL_TELEGRAM_BOT_TOKEN": os.environ["CHANNEL_TELEGRAM_BOT_TOKEN"],
            "CHANNEL_TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(16),
            "CHANNEL_TELEGRAM_REDIS_URL": res.redis_url,
            "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT": chat,
            "CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS": chat,
        }
    return {
        # Real bot tokens are ``<numeric bot id>:<secret>``; the plugin parses the
        # numeric prefix as its own identity, so the fake token mirrors the shape.
        "CHANNEL_TELEGRAM_BOT_TOKEN": "9900000000:e2e-telegram-bot-token",
        "CHANNEL_TELEGRAM_WEBHOOK_SECRET": secrets.token_hex(16),
        "CHANNEL_TELEGRAM_API_BASE_URL": _require_stub(res.telegram_api_base_url, "telegram"),
        "CHANNEL_TELEGRAM_REDIS_URL": res.redis_url,
        "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT": TELEGRAM_DEFAULT_RECIPIENT,
        "CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS": ",".join(TELEGRAM_ALLOWED_RECIPIENTS),
    }


def _slack_channel_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_CHANNEL_ID -> default + sole allowlisted recipient.
        # BOT_USER_ID is the operator-copied ``U…`` the bridge route's self-message
        # filter needs; passed through only when set (notify / ask_user / signature
        # verification need it not). No API_BASE_URL (default = real slack.com).
        channel = os.environ["CHANNEL_SLACK_TEST_CHANNEL_ID"]
        env = {
            "CHANNEL_SLACK_BOT_TOKEN": os.environ["CHANNEL_SLACK_BOT_TOKEN"],
            "CHANNEL_SLACK_SIGNING_SECRET": os.environ["CHANNEL_SLACK_SIGNING_SECRET"],
            "CHANNEL_SLACK_REDIS_URL": res.redis_url,
            "CHANNEL_SLACK_DEFAULT_RECIPIENT": channel,
            "CHANNEL_SLACK_ALLOWED_RECIPIENTS": channel,
        }
        if os.environ.get("CHANNEL_SLACK_BOT_USER_ID"):
            env["CHANNEL_SLACK_BOT_USER_ID"] = os.environ["CHANNEL_SLACK_BOT_USER_ID"]
        return env
    return {
        "CHANNEL_SLACK_BOT_TOKEN": "xoxb-e2e-slack-token",
        "CHANNEL_SLACK_SIGNING_SECRET": secrets.token_hex(16),
        "CHANNEL_SLACK_BOT_USER_ID": SLACK_BOT_USER_ID,
        "CHANNEL_SLACK_API_BASE_URL": _require_stub(res.slack_api_base_url, "slack"),
        "CHANNEL_SLACK_REDIS_URL": res.redis_url,
        "CHANNEL_SLACK_DEFAULT_RECIPIENT": SLACK_DEFAULT_RECIPIENT,
        "CHANNEL_SLACK_ALLOWED_RECIPIENTS": ",".join(SLACK_ALLOWED_RECIPIENTS),
    }


def _twilio_channel_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_TO -> default + sole allowlisted recipient; the whatsapp
        # sandbox leg boots the same stack with a ``whatsapp:``-prefixed FROM/TO (one
        # CHANNEL_TWILIO_FROM). No API_BASE_URL (default = real api.twilio.com).
        to = os.environ["CHANNEL_TWILIO_TEST_TO"]
        return {
            "CHANNEL_TWILIO_ACCOUNT_SID": os.environ["CHANNEL_TWILIO_ACCOUNT_SID"],
            "CHANNEL_TWILIO_AUTH_TOKEN": os.environ["CHANNEL_TWILIO_AUTH_TOKEN"],
            "CHANNEL_TWILIO_FROM": os.environ["CHANNEL_TWILIO_FROM"],
            "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
            "CHANNEL_TWILIO_DEFAULT_RECIPIENT": to,
            "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": to,
        }
    return {
        "CHANNEL_TWILIO_ACCOUNT_SID": "ACe2e00000000000000000000000000000",
        "CHANNEL_TWILIO_AUTH_TOKEN": secrets.token_hex(16),
        "CHANNEL_TWILIO_FROM": TWILIO_FROM,
        "CHANNEL_TWILIO_API_BASE_URL": _require_stub(res.twilio_api_base_url, "twilio"),
        "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
        "CHANNEL_TWILIO_DEFAULT_RECIPIENT": TWILIO_DEFAULT_RECIPIENT,
        "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": ",".join(TWILIO_ALLOWED_RECIPIENTS),
    }


def _web_channel_env(res: StackResources) -> dict[str, str]:
    """The web channel's env — the same on every leg: it has no vendor, so there is no
    stub base URL to point at and no real/mock split. Its own transcript store, the
    plain-http cookie relaxation (the harness serves the chat page over ``http``, and a
    ``Secure`` cookie is never stored there — every visitor would get a fresh session),
    and the limiter windows for its own public door family.

    ``/api/channels/web/*`` is a rate-limited PUBLIC family whose per-IP bucket collapses
    every harness client into one 127.0.0.1 entry, and one visitor action is several
    requests: a page load is the shell plus each bundle file it links, the stream-cap leg
    opens the per-visitor maximum at once, and the specs sharing one stack land in one
    window. The stock 120/min + 30/10s are not comfortably above that, so the windows are
    pinned high exactly as the interactions-callback family's are — the limiter stays ON,
    and no leg's determinism rests on the operator defaults. The per-visitor stream cap, the
    answer-restore cap and the POST body cap are pinned so their legs read them back off this
    env instead of restating the plugin's defaults."""
    return {
        "CHANNEL_WEB_REDIS_URL": res.redis_url,
        "CHANNEL_WEB_SESSION_COOKIE_SECURE": "false",
        "CHANNEL_WEB_MAX_STREAMS_PER_VISITOR": str(WEB_MAX_STREAMS_PER_VISITOR),
        "CHANNEL_WEB_MAX_ANSWER_RESTORES": str(WEB_MAX_ANSWER_RESTORES),
        "CHANNEL_WEB_MAX_BODY_BYTES": str(WEB_MAX_BODY_BYTES),
        "TAI_RATE_LIMIT_FAMILIES__CHANNELS_WEB__LIMIT": "100000",
        "TAI_RATE_LIMIT_FAMILIES__CHANNELS_WEB__BURST": "100000",
    }


def _require_stub(url: str | None, medium: str) -> str:
    """A MOCK medium needs its recording-stub base URL on resources (a REAL medium
    talks to the live vendor and needs none). A mock medium missing its stub is a
    mis-wired fixture, caught here rather than at boot."""
    if url is None:
        raise RuntimeError(
            f"build_channel_stack (mock {medium}) requires the {medium} stub base URL on resources; "
            "the channel_stack fixture allocates the stubs and passes them as resource_kwargs"
        )
    return url


def _channel_env(res: StackResources, variants: Variants) -> dict[str, str]:
    """The ``CHANNEL_*`` env for the channel profile: per-plugin bot credential, a random
    per-stack inbound secret, the API base URL pointed at that medium's recording stub,
    the correlation store on this stack's Redis DB, and the disjoint default/allowlist
    recipient policy. The two public-base-URL keys are filled at boot with replica B's
    origin (see ``replica_b_origin_env_keys``).

    Each of telegram / slack / twilio is independently mock-or-real (``TAI_E2E_REAL``):
    a real medium drops its stub base URL (plugin default = live vendor) and reads its
    credential + test recipient from the operator template. All-mock (default) is
    byte-for-byte today's env. web has no vendor at all, so it is always real."""
    switch = _switch()
    env = _base_env(res, variants)
    env.update(_telegram_channel_env(res, real=switch.is_real("telegram")))
    env.update(_slack_channel_env(res, real=switch.is_real("slack")))
    env.update(_twilio_channel_env(res, real=switch.is_real("twilio")))
    env.update(_web_channel_env(res))
    # Channel-loop answers forward through the interactions callback door, whose per-IP
    # rate limiter buckets all loopback traffic together. Pin its windows high so the
    # shared 127.0.0.1 bucket never trips on test volume (the limiter stays ON).
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__BURST"] = "100000"
    return env


def _channel_public_keys(switch: HarnessSettings) -> list[str]:
    """The public-base-URL env keys the channel stack routes to ``E2E_PUBLIC_BASE_URL``
    when a channel is real inbound: telegram's setWebhook origin
    (``CHANNEL_TELEGRAM_PUBLIC_BASE_URL``) when telegram is real, and the ask_user
    callback origin (``INTERACTIONS_PUBLIC_BASE_URL``) minted into a real medium's
    outbound whenever any channel is real. Empty on the all-mock default, so the
    loopback replica-B fill is unchanged."""
    keys: list[str] = []
    if switch.is_real("telegram"):
        keys.append("CHANNEL_TELEGRAM_PUBLIC_BASE_URL")
    if any(switch.is_real(seam) for seam in _CHANNEL_SEAMS):
        keys.append("INTERACTIONS_PUBLIC_BASE_URL")
    return keys


def build_channel_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, NO backend worker — the channel-plugin cross-worker loop home.

    Loads four channel plugins so one stack exercises telegram + slack + twilio + web:
    the first three each register a channel, a signed inbound door, and (telegram) a
    setWebhook hook; web registers its channel and its PUBLIC chat doors, whose credential
    is the visitor's session cookie. Two replicas give the deterministic act-on-A /
    inbound-on-B addressing the loop needs; ``run_backend=False`` makes the module honestly
    ``backendless``, so it runs on the default backend leg only. Auth off. Carries
    ``ask_user`` and ``notify_user`` plus the interactions callback door and the
    notifications read router.

    No conversations backend here, so web's message door (which bridges through
    ``conversations.accept``) has nothing to accept into: the web round trip through that
    door is the bridge suite's, and this stack carries web's ask/answer half only."""
    manifest = {
        "default_routers": "none",
        "channel_modules": [
            "tai42_channel_telegram.register",
            "tai42_channel_slack.register",
            "tai42_channel_twilio.register",
            "tai42_channel_web.register",
        ],
        "routers_modules": [
            "tai42_skeleton.routers.health",
            "tai42_skeleton.routers.tools",
            "tai42_skeleton.routers.config",
            "tai42_skeleton.routers.interactions",
            "tai42_skeleton.routers.notifications",
        ],
        # reload_config + notify_user project via ``api_tools`` (the notifications router
        # registers the notify_user op); ask_user loads as a builtin module. So tools[]
        # carries only the interactions entry.
        "tools": [_INTERACTIONS_ENTRY],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "notify_user", "reload_config"],
    }
    switch = _switch()
    return StackConfig(
        name="channel",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=_channel_env(res, variants),
        run_backend=False,
        run_metrics=False,
        auth=False,
        # Filled at boot with replica B's origin: the ask minted on A carries a callback
        # URL that resolves on B, and telegram's setWebhook URL points at B. Known only
        # once ports bind. A real inbound channel overrides its key to the public origin
        # (see ``public_base_url_env_keys``), so the vendor reaches it.
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL", "CHANNEL_TELEGRAM_PUBLIC_BASE_URL"],
        public_base_url_env_keys=_channel_public_keys(switch),
        public_base_url=switch.public_base_url,
    )

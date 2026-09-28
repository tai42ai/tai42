"""The bridge stack profile and its per-medium env."""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
from typing import TYPE_CHECKING

from tai42_e2e.manifests.channels import (
    _require_stub,
    _slack_channel_env,
    _telegram_channel_env,
    _web_channel_env,
)
from tai42_e2e.manifests.feature_env import _base_env, _llm_env, _memory_agent_state_env, _switch
from tai42_e2e.manifests.tool_entries import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# Twilio deployment numbers (a channel route's ``our_identity`` = the number we are texted
# at). A and B are two numbers under the one fake account — the multi-identity leg.
BRIDGE_TWILIO_ACCOUNT_SID = "ACe2ebridge000000000000000000000000"


BRIDGE_TWILIO_FROM = "+15550100001"


BRIDGE_TWILIO_FROM_B = "+15550100002"


# The human on the far end of a twilio conversation (the ``client_address``). Doubles as the
# ask default recipient so a pending ask and a bridge turn share one number pair.
BRIDGE_TWILIO_CLIENT = "+15559001111"


BRIDGE_TWILIO_CLIENT_B = "+15559002222"


# WhatsApp phone_number_ids (a channel route's ``our_identity``).
BRIDGE_WHATSAPP_PHONE_ID = "111000111000111"


BRIDGE_WHATSAPP_PHONE_ID_B = "222000222000222"


BRIDGE_WHATSAPP_PHONE_ID_C = "333000333000333"


# The human wa_id on the far end (the ``client_address``); allowlisted so an ask over
# whatsapp can deliver to it.
BRIDGE_WHATSAPP_CLIENT = "15559003333"


# The WhatsApp Business Account id owning Flows — the form-delivery path creates a
# form ask's Flow under it (only the mock leg needs a fixed value; a real leg reads
# the operator's own WABA from env).
BRIDGE_WHATSAPP_WABA_ID = "waba000111000111"


# Cost-cap bounds: max concurrent turns per worker; max turns per client_address per hour.
BRIDGE_MAX_CONCURRENT_TURNS = 4


BRIDGE_PER_ADDRESS_TURNS_PER_HOUR = 5


# Live-caller sync-door acquire bound, pinned LOW: a bounded door contending for a thread an
# in-flight turn on the sibling worker still holds refuses with a fast, deterministic
# ThreadBusyError (503) instead of waiting out the 30s default. Only fires under contention —
# an uncontended door acquires immediately regardless of this value.
BRIDGE_SYNC_DOOR_WAIT_SECONDS = 2


# Media-ingest bounds for the inbound-media suites, pinned SMALL so the over-cap image case uses a
# small body (a few KB over the cap) instead of multi-MB fixtures, while a tiny valid PNG/PDF
# clears it. The reaper interval is pinned LOW so a retention-reaper pass lands within a test.
BRIDGE_MEDIA_INGEST_MAX_IMAGE_BYTES = 8192


BRIDGE_MEDIA_INGEST_REAPER_INTERVAL_SECONDS = 1


# Media-ingest bounds for the WEB upload suite. The image cap is pinned SMALL (so a slightly-over PNG
# is a few KB, not multi-MB), and the other kinds are pinned so ``max_cap`` — which the web upload
# door declares as its request-body bound — is small enough that a body a few KB larger is refused by
# the body-limit backstop BEFORE it reaches the door, while an over-image-cap PNG UNDER that bound
# still reaches the ingest seam and is refused there.
WEB_MEDIA_INGEST_MAX_IMAGE_BYTES = 8192


WEB_MEDIA_INGEST_MAX_OTHER_BYTES = 16384


# Seconds an unreferenced upload is held PENDING before its horizon lapses (bind refuses, the reaper
# reclaims it), pinned SHORT on the EXPIRY stack only so that leg lands within a test; the reaper
# cadence pinned LOW there so its reclamation does too. Every other web-media leg keeps the operator
# default, so an upload followed by a send never races the reaper.
WEB_MEDIA_EXPIRY_PENDING_TTL_SECONDS = 3


WEB_MEDIA_EXPIRY_REAPER_INTERVAL_SECONDS = 1


# The working-signal profile's hard ceiling on ONE turn's refresh loop, pinned LOW on the
# CAPPED leg so the ceiling stops the loop after a couple of refreshes even on a long-held
# turn — the observable a low ``CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS`` produces.
WORKING_SIGNAL_CAPPED_MAX_SECONDS = 1.2


# Every channel a working-signal profile can deliver a bridged answer on needs a split cap, or
# the send fails as a config error. The vendor mediums keep their platform defaults; the two
# stub channels are added so the loop's counting stub delivers its turn's answer.
_WORKING_SIGNAL_MAX_MESSAGE_CHARS = {
    "twilio": 1600,
    "telegram": 4096,
    "slack": 40000,
    "whatsapp": 4096,
    "web": 8000,
    "stub_working": 8000,
    "stub_working_off": 8000,
}


def _bridge_twilio_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: TEST_TO -> default + sole allowlisted recipient; no API_BASE_URL
        # (plugin default = real api.twilio.com).
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
        "CHANNEL_TWILIO_ACCOUNT_SID": BRIDGE_TWILIO_ACCOUNT_SID,
        "CHANNEL_TWILIO_AUTH_TOKEN": secrets.token_hex(16),
        "CHANNEL_TWILIO_FROM": BRIDGE_TWILIO_FROM,
        "CHANNEL_TWILIO_API_BASE_URL": _require_stub(res.twilio_api_base_url, "twilio"),
        "CHANNEL_TWILIO_REDIS_URL": res.redis_url,
        "CHANNEL_TWILIO_DEFAULT_RECIPIENT": BRIDGE_TWILIO_CLIENT,
        "CHANNEL_TWILIO_ALLOWED_RECIPIENTS": ",".join([BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_CLIENT_B]),
    }


def _bridge_whatsapp_env(res: StackResources, *, real: bool) -> dict[str, str]:
    if real:
        # HARNESS-MAP: DEFAULT_PHONE_NUMBER_ID is our_identity, TEST_TO the allowlisted
        # wa_id; the VERIFY_TOKEN is operator-set (shared with Meta's dashboard
        # subscription). No API_BASE_URL (plugin default = real graph.facebook.com).
        return {
            "CHANNEL_WHATSAPP_ACCESS_TOKEN": os.environ["CHANNEL_WHATSAPP_ACCESS_TOKEN"],
            "CHANNEL_WHATSAPP_APP_SECRET": os.environ["CHANNEL_WHATSAPP_APP_SECRET"],
            "CHANNEL_WHATSAPP_VERIFY_TOKEN": os.environ["CHANNEL_WHATSAPP_VERIFY_TOKEN"],
            "CHANNEL_WHATSAPP_REDIS_URL": res.redis_url,
            "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID": os.environ["CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"],
            "CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS": os.environ["CHANNEL_WHATSAPP_TEST_TO"],
        }
    return {
        "CHANNEL_WHATSAPP_ACCESS_TOKEN": "e2e-whatsapp-access-token",
        "CHANNEL_WHATSAPP_APP_SECRET": secrets.token_hex(16),
        "CHANNEL_WHATSAPP_VERIFY_TOKEN": secrets.token_hex(16),
        "CHANNEL_WHATSAPP_API_BASE_URL": _require_stub(res.whatsapp_api_base_url, "whatsapp"),
        "CHANNEL_WHATSAPP_REDIS_URL": res.redis_url,
        "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID": BRIDGE_WHATSAPP_PHONE_ID,
        # The WABA a form ask's WhatsApp Flow is created under (form-delivery path only).
        "CHANNEL_WHATSAPP_WABA_ID": BRIDGE_WHATSAPP_WABA_ID,
        "CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS": BRIDGE_WHATSAPP_CLIENT,
    }


def _bridge_channel_env(res: StackResources) -> dict[str, str]:
    """The twilio + whatsapp + web ``CHANNEL_*`` env for the bridge profile: per-plugin
    credential, a random per-stack inbound secret, the API base URL pointed at that medium's
    recording stub, the correlation store on this stack's Redis DB, and the ask
    recipient policy (a default twilio recipient; an allowlisted whatsapp wa_id).

    twilio / whatsapp are independently mock-or-real (``TAI_E2E_REAL``): a real medium
    drops its stub base URL (plugin default = live vendor) and reads its credential +
    test recipient from the operator template. All-mock (default) is byte-for-byte
    today's env. web has no vendor at all, so it is always real."""
    switch = _switch()
    env = _bridge_twilio_env(res, real=switch.is_real("twilio"))
    env.update(_bridge_whatsapp_env(res, real=switch.is_real("whatsapp")))
    env.update(_web_channel_env(res))
    return env


def build_bridge_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + metrics, access control ON — the messaging-bridge home.

    Carries the redis conversations backend (``CONVERSATIONS_REDIS_URL``), the memory
    checkpoint provider (conversation continuity lives in the serve worker that ran the
    turn, so a spec pins its inbound fires to one replica), the twilio + whatsapp + web
    channel plugins (twilio/whatsapp outbound pointed at their in-process stubs; web has no
    vendor — its public chat page, message door and SSE stream ARE the medium), and the
    ``tools_agent`` + ``langchain_deep_agent`` agents on the scripted LLM stub. Access control is ON so
    the API door resolves a caller principal and the turn runs AS a route's bound execution
    key; the ``bridge_stack`` fixture seeds the root key + the public-channel-door route
    table before boot."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "channel_modules": [
            "tai42_channel_twilio.register",
            "tai42_channel_whatsapp.register",
            "tai42_channel_web.register",
        ],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.conversations",
            "tai42_skeleton.routers.checkpoints",
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.agents",
            "tai42_skeleton.routers.notifications",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True),
            *_builtin_entries(),
            # The pairing-code mint builtin, opted in as a tool[].module row exactly as a
            # deployment does: the bridge suite drives it as a tool-target route to prove the
            # {code, expires_at} contract end to end.
            {"title": "builtin-pairing", "module": "tai42_skeleton.tools.builtin.get_pairing_code"},
            # The in-process conversation door tools (send_conversation_message / _event): a run
            # posts to a route or an existing thread under the deployment's own identity.
            {"title": "builtin-doors", "module": "tai42_skeleton.tools.builtin.doors"},
        ],
        "agents": [
            {"title": "tai-agents-tools", "module": "tai42_agents.tools_agent", "include": ["tools_agent"]},
            {
                "title": "tai-agents-deep",
                "module": "tai42_agents.langchain_deep_agent",
                "include": ["langchain_deep_agent"],
            },
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        # notify_user rides the mounted notifications router (projected via api_tools): the
        # bridge suite drives it for the whatsapp media/template and recipient-policy legs.
        "user_tools": ["ask", "notify_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env["CONVERSATIONS_REDIS_URL"] = res.redis_url
    env["CONVERSATIONS_PREFIX"] = f"{res.bus_namespace}:conversations"
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    env.update(_bridge_channel_env(res))
    # Small delivery ceiling + backoff so an undeliverable answer reaches terminal ``failed`` fast.
    env["CONVERSATIONS_DELIVERY_MAX_ATTEMPTS"] = "2"
    env["CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS"] = "1"
    env["CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS"] = "1"
    # Cost caps pinned low: global in-flight-turn ceiling and per-address per-hour turn rate.
    env["CONVERSATIONS_MAX_CONCURRENT_TURNS"] = str(BRIDGE_MAX_CONCURRENT_TURNS)
    env["CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR"] = str(BRIDGE_PER_ADDRESS_TURNS_PER_HOUR)
    # Sync-door acquire bound pinned low so a cross-worker contended door refuses fast.
    env["CONVERSATIONS_SYNC_DOOR_WAIT_SECONDS"] = str(BRIDGE_SYNC_DOOR_WAIT_SECONDS)
    # Loopback callbacks share one 127.0.0.1 bucket; pin the limiter windows high so test
    # volume never trips it.
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__BURST"] = "100000"
    switch = _switch()
    # A real inbound channel (twilio/whatsapp) mints the ask callback into its
    # outbound over the public origin instead of replica-B loopback; empty on all-mock.
    bridge_public_keys = (
        ["INTERACTIONS_PUBLIC_BASE_URL"] if any(switch.is_real(s) for s in ("twilio", "whatsapp")) else []
    )
    return StackConfig(
        name="bridge",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
        public_base_url_env_keys=bridge_public_keys,
        public_base_url=switch.public_base_url,
    )


def build_media_bridge_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The bridge profile carrying ALL FOUR vendor channels — telegram + slack alongside
    twilio + whatsapp (and web) — for the inbound-media bridge and unsupported-inbound suites.

    Identical to :func:`build_bridge_stack` (backend + metrics + the redis conversations backend +
    the hooks manager an unsupported event is observed through) with the telegram + slack channel
    plugins added, so one stack bridges a media inbound to a turn AND replies + emits the platform
    rejection event on every channel. Telegram's ``setWebhook`` needs its inbound-door origin, so
    ``CHANNEL_TELEGRAM_PUBLIC_BASE_URL`` joins the replica-B origin fill; the shared
    ``^/api/channels/.*$`` public route already covers the telegram + slack inbound doors."""
    base = build_bridge_stack(res, variants)
    switch = _switch()
    manifest = {
        **base.manifest,
        "channel_modules": [
            *base.manifest["channel_modules"],
            "tai42_channel_telegram.register",
            "tai42_channel_slack.register",
        ],
    }
    # The blob provider the ingest chokepoint writes through is already live on the base stack (its
    # ``storage_module`` = ``variants.storage.module`` registers ``tai42_storage_local``, rooted at
    # ``STORAGE_LOCAL_ROOT_PATH`` from ``_base_env``); this profile only pins the ingest caps + the
    # reaper cadence the media suites need.
    env = {
        **base.env,
        **_telegram_channel_env(res, real=switch.is_real("telegram")),
        **_slack_channel_env(res, real=switch.is_real("slack")),
        "MEDIA_INGEST_MAX_IMAGE_BYTES": str(BRIDGE_MEDIA_INGEST_MAX_IMAGE_BYTES),
        "MEDIA_INGEST_REAPER_INTERVAL_SECONDS": str(BRIDGE_MEDIA_INGEST_REAPER_INTERVAL_SECONDS),
    }
    return dataclasses.replace(
        base,
        name="media-bridge",
        manifest=manifest,
        env=env,
        replica_b_origin_env_keys=[*base.replica_b_origin_env_keys, "CHANNEL_TELEGRAM_PUBLIC_BASE_URL"],
    )


def build_media_bridge_no_store_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The media-bridge profile with NO blob provider registered — the store-unavailable leg.

    Identical to :func:`build_media_bridge_stack` but with the ``storage_module`` dropped from the
    manifest, so the storage registry stays dead by default and the ingest chokepoint raises
    ``MediaStoreUnavailableError`` — the channel then rejects the media with the
    ``could_not_receive`` notice + event. Every other seam (channels, conversations backend, hooks)
    is unchanged, so only the missing provider drives the outcome."""
    base = build_media_bridge_stack(res, variants)
    manifest = {key: value for key, value in base.manifest.items() if key != "storage_module"}
    return dataclasses.replace(base, name="media-bridge-nostore", manifest=manifest)


def build_web_media_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The bridge profile tuned for the WEB inbound-media (upload door + attachment bridging) suite.

    Identical to :func:`build_bridge_stack` (the web channel + its public chat doors, the redis
    conversations backend a bridged turn accepts into, and the ``storage_module`` blob provider the
    ingest chokepoint writes through — already live on the base stack) with only the ``MEDIA_INGEST_*``
    byte caps pinned: every per-kind cap is small so the upload door's declared body bound
    (``max_cap``) is small. The pending-upload TTL and the reaper cadence stay at the operator
    defaults, so an upload followed by a send never races the reaper; the expiry leg runs on
    :func:`build_web_media_expiry_stack`."""
    base = build_bridge_stack(res, variants)
    env = {
        **base.env,
        "MEDIA_INGEST_MAX_IMAGE_BYTES": str(WEB_MEDIA_INGEST_MAX_IMAGE_BYTES),
        "MEDIA_INGEST_MAX_AUDIO_BYTES": str(WEB_MEDIA_INGEST_MAX_OTHER_BYTES),
        "MEDIA_INGEST_MAX_VIDEO_BYTES": str(WEB_MEDIA_INGEST_MAX_OTHER_BYTES),
        "MEDIA_INGEST_MAX_DOCUMENT_BYTES": str(WEB_MEDIA_INGEST_MAX_OTHER_BYTES),
        # The upload door charges its own ``channels_web_uploads`` limiter family; its stock 20/min +
        # 5 burst is below the many uploads the suite's specs make against the shared 127.0.0.1
        # bucket, so pin its windows high exactly as ``_web_channel_env`` pins ``channels_web`` — the
        # limiter stays ON, no leg's determinism rests on the operator defaults.
        "TAI_RATE_LIMIT_FAMILIES__CHANNELS_WEB_UPLOADS__LIMIT": "100000",
        "TAI_RATE_LIMIT_FAMILIES__CHANNELS_WEB_UPLOADS__BURST": "100000",
    }
    return dataclasses.replace(base, name="web-media", env=env)


def build_web_media_expiry_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The web-media profile with a SHORT pending-upload TTL — the unreferenced-upload expiry leg.

    Identical to :func:`build_web_media_stack` with the pending TTL pinned short so an unreferenced
    upload's horizon lapses within a test, and the reaper cadence pinned low so its reclamation does
    too. Kept apart from the shared profile because a short TTL races every upload-then-send leg
    against the reaper."""
    base = build_web_media_stack(res, variants)
    env = {
        **base.env,
        "MEDIA_INGEST_PENDING_TTL_SECONDS": str(WEB_MEDIA_EXPIRY_PENDING_TTL_SECONDS),
        "MEDIA_INGEST_REAPER_INTERVAL_SECONDS": str(WEB_MEDIA_EXPIRY_REAPER_INTERVAL_SECONDS),
    }
    return dataclasses.replace(base, name="web-media-expiry", env=env)


def build_working_signal_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The bridge profile carrying the working-signal counting stub channels.

    Identical to :func:`build_bridge_stack` (access control ON, the redis conversations backend a
    bridged turn accepts into, the probe tools whose ``e2e_channel_inbound`` drives a stub inbound
    and whose ``e2e_overlap_probe`` holds a turn open) with the ``tai42_e2e_fixtures.stub_channel``
    module added, which registers ``stub_working`` (a vendor indicator the loop refreshes) and
    ``stub_working_off`` (no indicator — the loop never starts). The two stub channels are added to
    ``CONVERSATIONS_MAX_MESSAGE_CHARS`` so a bridged answer on them delivers. The working-signal
    ceiling stays at the operator default, so the loop stops at the first outbound send, not the
    ceiling — the leg that proves the refresh-then-stop behaviour."""
    base = build_bridge_stack(res, variants)
    manifest = {
        **base.manifest,
        "channel_modules": [*base.manifest["channel_modules"], "tai42_e2e_fixtures.stub_channel"],
    }
    env = {
        **base.env,
        "CONVERSATIONS_MAX_MESSAGE_CHARS": json.dumps(_WORKING_SIGNAL_MAX_MESSAGE_CHARS),
    }
    return dataclasses.replace(base, name="working-signal", manifest=manifest, env=env)


def build_working_signal_capped_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The working-signal profile with a LOW refresh ceiling — the ceiling-caps-the-count leg.

    Identical to :func:`build_working_signal_stack` with ``CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS``
    pinned low, so the loop stops at the ceiling after a couple of refreshes even while the turn is
    still held open — the count a long turn would otherwise keep growing is capped. Kept apart from
    the default-ceiling profile because a low ceiling would pre-empt that profile's stop-at-send
    leg."""
    base = build_working_signal_stack(res, variants)
    env = {
        **base.env,
        "CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS": str(WORKING_SIGNAL_CAPPED_MAX_SECONDS),
    }
    return dataclasses.replace(base, name="working-signal-capped", env=env)


def build_web_media_no_store_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The web-media profile with NO blob provider registered — the upload store-unavailable leg.

    Identical to :func:`build_web_media_stack` but with the ``storage_module`` dropped from the
    manifest, so the storage registry stays dead and the ingest chokepoint raises
    ``MediaStoreUnavailableError`` — the upload door then answers 503 ``media_store_unavailable``.
    Only the missing provider drives the outcome."""
    base = build_web_media_stack(res, variants)
    manifest = {key: value for key, value in base.manifest.items() if key != "storage_module"}
    return dataclasses.replace(base, name="web-media-nostore", manifest=manifest)

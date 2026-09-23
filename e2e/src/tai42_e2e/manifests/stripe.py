"""The Stripe integration stack profile."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _switch
from tai42_e2e.manifests.tool_entries import _CORE_ROUTERS, _PROJECTED_API_TOOLS, _builtin_entries
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# The bridge/door shared-secret verifier the composed ask binds per question: header-based
# (NOT ``post_only``), reading ``TAI_BRIDGE_CALLBACK_SECRET`` at the door. Author-bound as
# the ask_external extension's ``config.verifier``, so an agent can never supply it.
_STRIPE_ASK_VERIFIER = {
    "name": "shared_secret",
    "config": {"header": "X-TAI-Bridge-Secret", "secret_env": "TAI_BRIDGE_CALLBACK_SECRET"},
}


# A dummy Stripe secret key. The ``sk_test_`` prefix is load-bearing: ``_expected_livemode``
# reads it and it must agree with the FakeStripe stub's ``livemode: false`` on every session.
_STRIPE_TEST_SECRET_KEY = "sk_test_e2e0000000000000000000000000"


def build_stripe_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS, access control ON, NO backend/metrics — the Stripe payments home.

    Loads the ``stripe`` webhook verifier (canonical ``webhook_verifier_modules``), the
    built-in ``shared_secret`` verifier (the composed ask's per-question binding resolves
    it), the identity provider (access control resolves a caller principal), the
    ``ask_external`` extension, and all three stripe tool modules — the builder composed
    with ``ask_external``, plus the bridge and the reconciler for the recovery leg. The
    ``stripe_stack`` fixture seeds a root key + the public webhook/callback route table
    before boot, and the FakeStripe stub's origin, the two secrets and the api key ride in
    as resources.

    ``INTERACTIONS_PUBLIC_BASE_URL`` is filled at boot with replica B's origin so the
    callback URL the platform mints is DIALABLE by the bridge running inside the stack (the
    bridge's SSRF pin compares against this same value). The callback rate-limit windows are
    pinned high: the webhook loop, the forged-session rejection, and a reconciliation run
    that re-answers everything all share one 127.0.0.1 bucket."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module, "tai42_skeleton.webhooks.builtin.shared_secret"],
        "webhook_verifier_modules": ["tai42_webhook_verifier_stripe"],
        # The api_keys router mounts POST /api/auth/api-keys: the hook's execution key must be
        # a minted key's user_id (every mint stamps the fingerprint the bind resolves), so the
        # payments leg mints one and binds it. _CORE_ROUTERS carries hooks/interactions/presets.
        "routers_modules": [*_CORE_ROUTERS, "tai42_skeleton.routers.api_keys"],
        "extensions_modules": ["tai42_skeleton.extensions.builtin.ask_external"],
        "storage_module": variants.storage.module,
        "tools": [
            # The builder, composed with ask_external into create_stripe_checkout_ask_external:
            # the author-bound verifier is the combo element's config, out of the agent's reach.
            {
                "title": "stripe-checkout",
                "module": "tai42_tools_stripe.tools.create_stripe_checkout",
                "extensions": {
                    "create_stripe_checkout": [[{"name": "ask_external", "config": {"verifier": _STRIPE_ASK_VERIFIER}}]]
                },
            },
            # The hook's bridge target and the recovery-layer reconciler: registered so the
            # hook and an authed reconcile call can each resolve them by name.
            {"title": "stripe-confirm", "module": "tai42_tools_stripe.tools.confirm_stripe_payment"},
            {"title": "stripe-reconcile", "module": "tai42_tools_stripe.tools.reconcile_stripe_payments"},
            # The flexible-amount payment-link builder — a fire-and-return hosted link with no
            # ask/callback. It mints a Checkout Session through the SAME client seam as the
            # checkout builder, so the FakeStripe stub answers it and no run-time call reaches
            # a real Stripe host. Kept off every user/agent surface (see ``user_tools`` below).
            {"title": "stripe-payment-link", "module": "tai42_tools_stripe.tools.create_stripe_payment_link"},
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        # The four stripe names are kept off the user surface (they are answer capabilities);
        # agents are given the money-pinned preset over the composed tool, never these.
        "user_tools": ["ask", "reload_config"],
    }
    switch = _switch()
    stripe_real = switch.is_real("stripe")
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    # Stripe tool config. MOCK: the api base points at the in-process FakeStripe stub and
    # the test-mode key agrees with the stub's livemode. REAL: drop the api base (plugin
    # default = real api.stripe.com) and read the operator's ``sk_test_`` key.
    if stripe_real:
        env["STRIPE_SECRET_KEY"] = os.environ["STRIPE_SECRET_KEY"]
    else:
        if res.stripe_stub_base is not None:
            env["STRIPE_API_BASE"] = res.stripe_stub_base
        env["STRIPE_SECRET_KEY"] = _STRIPE_TEST_SECRET_KEY
    # The topic's stripe verifier reads this env name; MOCK signs deliveries with the
    # harness-minted secret, REAL feeds the dashboard endpoint's ``whsec_`` signing secret.
    if stripe_real:
        env["E2E_STRIPE_WEBHOOK_SECRET"] = os.environ["STRIPE_WEBHOOK_SECRET"]
    elif res.stripe_webhook_secret is not None:
        env["E2E_STRIPE_WEBHOOK_SECRET"] = res.stripe_webhook_secret
    # One value read by BOTH the door's shared_secret verifier and the bridge tool.
    if res.bridge_callback_secret is not None:
        env["TAI_BRIDGE_CALLBACK_SECRET"] = res.bridge_callback_secret
    # Loopback callbacks share one 127.0.0.1 bucket; pin the limiter windows high so the
    # webhook loop + forged rejection + reconciliation volume never trips it.
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__LIMIT"] = "100000"
    env["TAI_RATE_LIMIT_FAMILIES__INTERACTIONS_CALLBACK__BURST"] = "100000"
    return StackConfig(
        name="notifications",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=False,
        run_metrics=False,
        auth=True,
        # Filled at boot with replica B's origin: the callback URL minted on A is dialable
        # by the bridge, and the SSRF pin's ground truth is this same value. Real Stripe
        # delivers ``checkout.session.completed`` to the public origin, so the callback the
        # bridge answers is minted there instead of loopback.
        replica_b_origin_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"],
        public_base_url_env_keys=["INTERACTIONS_PUBLIC_BASE_URL"] if stripe_real else [],
        public_base_url=switch.public_base_url,
    )

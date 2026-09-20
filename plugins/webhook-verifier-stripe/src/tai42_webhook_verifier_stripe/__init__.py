"""Stripe webhook-signature verifier plugin.

Importing this package registers a :class:`StripeWebhookVerifier` under the name
``"stripe"`` on the app handle's ``webhook_verifiers`` facet, so a webhook door can
bind ``stripe`` to a topic. Import-only: the ``webhook-verifier`` kind's own
``webhook_verifier_modules`` manifest field lists this module and the host imports it
purely for the registration side effect. Depends only on ``tai42-contract``.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_webhook_verifier_stripe.verifier import StripeWebhookVerifier

# Registration side effect run on import; a duplicate name raises loudly.
tai42_app.webhook_verifiers.register("stripe", StripeWebhookVerifier())

__all__ = ["StripeWebhookVerifier"]

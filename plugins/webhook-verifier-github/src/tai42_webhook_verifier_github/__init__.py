"""GitHub webhook-signature verifier plugin.

Importing this package registers a :class:`GitHubWebhookVerifier` under the name
``"github"`` on the app handle's ``webhook_verifiers`` facet, so a webhook door can
bind ``github`` to a topic. Import-only: loaded via the manifest's
``lifecycle_modules`` purely for the registration side effect. Depends only on
``tai42-contract``.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_webhook_verifier_github.verifier import GitHubWebhookVerifier

# Registration side effect run on import; a duplicate name raises loudly.
tai42_app.webhook_verifiers.register("github", GitHubWebhookVerifier())

__all__ = ["GitHubWebhookVerifier"]

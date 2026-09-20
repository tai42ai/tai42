"""The Slack inbound doors — the Events API door and the interactivity door.

Both declare ``public: true`` (Slack cannot present the deployment api key) and both
authenticate by the ``X-Slack-Signature`` v0 HMAC over the exact raw body, reading a
bounded body first (:mod:`verification`). Correlated replies and uncorrelated bridging
share one routing layer (:mod:`routing`); the Events API door (:mod:`events`,
``/inbound``) and the interactivity door (:mod:`interactive`, ``/interactive``) hold
their own decoders and register their routes.

Importing this package registers both doors as a side effect via the imports below.
"""

from tai42_channel_slack.inbound import events, interactive  # noqa: F401  (route-registration side-effect)

"""A fixture router proving RS-B6: a surviving epoch's pre-auth login route resolves the
LIVE epoch's provider (non-500) after a FAILED build.

At import it registers a TEST-LOCAL fake identity provider under :data:`PROVIDER_NAME`
(so each epoch build re-fires the registration into that generation's staged registry,
exactly as a real identity-provider plugin does) and mounts a pre-auth route that resolves
the current epoch's live provider through the SAME contract accessor the real
accounts-oidc login routes use — ``tai42_app.accounts.active_provider`` — which forwards to
the current epoch's ``ServingCore.active_auth_providers``. The route answers 200 naming the
resolved provider, or 500 when none is active: a module holder left pointing at a discarded
or half-built generation would surface as a 500, so the test's non-500 assertion is
meaningful. No tai42-accounts-oidc dependency is needed — the fake provider through the real
epoch machinery exercises the mechanism end to end.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider
from tai42_contract.access_control.registry import register_identity_provider
from tai42_contract.app import tai42_app

PROVIDER_NAME = "fake_preauth"
PREAUTH_PATH = "/api/preauth-probe"


class _FakePreAuthProvider(IdentityProvider):
    """A minimal identity provider the epoch probe instantiates, records, and
    healthchecks; the healthcheck is the inherited no-op, so boot never reaches a store."""

    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None


def _factory(_settings: object) -> IdentityProvider:
    return _FakePreAuthProvider()


register_identity_provider(PROVIDER_NAME, _factory)


@tai42_app.http.custom_route(
    PREAUTH_PATH,
    methods=["GET"],
    summary="Pre-auth probe resolving the live epoch's provider.",
    tags=["test"],
    response_model=None,
    authed=False,
)
async def preauth_probe(_request: Request) -> JSONResponse:
    # The same resolution path the real accounts-oidc pre-auth routes take: read the
    # CURRENT epoch's live provider instance, never a module-level holder.
    provider = tai42_app.accounts.active_provider(PROVIDER_NAME)
    if provider is None:
        # No live provider for the surviving epoch — the failure mode RS-B6 guards
        # against (a holder left pointing at a discarded/half-built generation).
        return JSONResponse({"error": "no active provider"}, status_code=500)
    return JSONResponse({"provider": type(provider).__name__})

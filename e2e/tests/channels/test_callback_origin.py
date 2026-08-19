"""``callback_origin`` binds a signed inbound to the RIGHT base URL.

The signed-URL medium (twilio) computes its X-Twilio-Signature over the origin the
inbound door reconstructs. Mock legs reach the door over replica-B loopback; a real
inbound leg reaches it over the public origin. The gate is THIS case's OWN medium
running real (``settings.is_real(self.name)``), NOT the stack-wide public-base-URL
routing: ``public_base_url_env_keys`` carries ``INTERACTIONS_PUBLIC_BASE_URL`` whenever
ANY channel seam is real, so in a mixed session (real telegram + mock twilio) gating on
that key would make the mock twilio leg sign over the public origin while POSTing to
loopback — a 401. And ``public_base_url`` alone can't gate it either: the manifest
carries the ambient ``E2E_PUBLIC_BASE_URL`` onto every leg, mock included. These pin
that a mock twilio leg stays loopback whatever else is real and whether or not
``E2E_PUBLIC_BASE_URL`` is set.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

from ._support import TwilioCase  # pyright: ignore[reportMissingImports]


def _case(*, public_base_url: str | None, real: Iterable[str] = ()) -> TwilioCase:
    real_services = frozenset(real)
    settings = SimpleNamespace(is_real=lambda service: service in real_services)
    config = SimpleNamespace(public_base_url=public_base_url)
    stack = SimpleNamespace(config=config, infra=SimpleNamespace(settings=settings), host="127.0.0.1", port_b=8002)
    return TwilioCase(stack, None)  # type: ignore[arg-type]


def test_mock_leg_binds_loopback_when_public_base_url_unset() -> None:
    # The all-mock default: no public origin at all -> loopback.
    case = _case(public_base_url=None)
    assert case.callback_origin == "http://127.0.0.1:8002"


def test_mock_leg_binds_loopback_even_with_public_base_url_set() -> None:
    # The e2e host always sets E2E_PUBLIC_BASE_URL, so the manifest carries
    # public_base_url onto the mock leg too — but this medium is mock. The mock twilio
    # door still binds to loopback, so its X-Twilio-Signature matches the loopback POST
    # (never a 401).
    case = _case(public_base_url="https://e2e.example")
    assert case.callback_origin == "http://127.0.0.1:8002"


def test_mixed_session_other_channel_real_keeps_twilio_loopback() -> None:
    # The mixed-session bug: a DIFFERENT channel runs real (real telegram), so the stack
    # routes INTERACTIONS_PUBLIC_BASE_URL stack-wide — yet twilio itself is mock and its
    # door reaches over loopback. Gating on this medium's own real leg keeps it loopback,
    # so its signature matches the loopback POST.
    case = _case(public_base_url="https://e2e.example", real=("telegram",))
    assert case.callback_origin == "http://127.0.0.1:8002"


def test_real_inbound_leg_binds_public_origin() -> None:
    # twilio real: the door binds to the public origin the vendor signs over (trailing
    # slash trimmed).
    case = _case(public_base_url="https://e2e.example/", real=("twilio",))
    assert case.callback_origin == "https://e2e.example"

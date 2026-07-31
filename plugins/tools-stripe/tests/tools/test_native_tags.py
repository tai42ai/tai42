"""Every Stripe tool declares its native ``stripe``/``payments`` tags at registration."""

from __future__ import annotations

import pytest

# Importing the tool modules runs their registration through the null app bound in
# conftest, which records the declared tags.
from tai42_tools_stripe.tools import (  # noqa: F401
    confirm_stripe_payment,
    create_stripe_checkout,
    create_stripe_payment_link,
    reconcile_stripe_payments,
)

_STRIPE_TOOLS = [
    "confirm_stripe_payment",
    "create_stripe_checkout",
    "create_stripe_payment_link",
    "reconcile_stripe_payments",
]


@pytest.mark.parametrize("name", _STRIPE_TOOLS)
def test_stripe_tool_tagged(name: str, null_app) -> None:
    assert null_app.tools.tags[name] == {"stripe", "payments"}

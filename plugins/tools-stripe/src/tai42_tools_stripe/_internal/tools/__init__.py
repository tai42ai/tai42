"""Private helper modules backing the Stripe tools.

Not part of the public API: the module here holds the shared Stripe REST client (Checkout Session
create/list, the livemode assert, the exact-origin SSRF pin and the bounded callback-door retry)
that the registered entrypoints in ``tai42_tools_stripe.tools`` delegate to. Nothing here registers
through ``tai42_app``.
"""

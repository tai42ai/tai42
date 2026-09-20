"""Private helper modules backing the Stripe tools.

Not part of the public API. ``tools`` holds the shared Stripe REST client and callback bridge that
the thin registered entrypoints in ``tai42_tools_stripe.tools`` delegate to. Nothing here registers
through ``tai42_app``.
"""

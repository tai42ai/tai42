"""tai42-tools-stripe — Stripe payment tools for the TAI ecosystem.

An opt-in, manifest-loaded collection of Stripe Checkout tools: the external-ask checkout link,
the webhook-bridge confirm, the reconciliation recovery layer, and a flexible-amount non-blocking
payment link. Nothing here is imported at package import time: each module registers its tool
through the global ``tai42_app`` handle and is loaded by the host via the manifest's
``tools[].module`` field.
"""

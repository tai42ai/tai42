"""tai42-tools-stripe — Stripe payment tools for the TAI ecosystem.

An opt-in, manifest-loaded collection of Stripe tools: the external-ask checkout link, the
webhook-bridge confirm, the reconciliation recovery layer, a flexible-amount non-blocking payment
link, an early expire for an issued checkout session, and webhook-endpoint provisioning (create,
list, delete). Nothing here is imported at package import time: each module registers its tool
through the global ``tai42_app`` handle and is loaded by the host via the manifest's
``tools[].module`` field.
"""

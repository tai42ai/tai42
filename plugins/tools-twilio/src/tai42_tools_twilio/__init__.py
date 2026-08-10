"""tai42-tools-twilio — Twilio provisioning tools for the TAI ecosystem.

An opt-in, manifest-loaded collection of Twilio provider-side provisioning tools:
list an account's incoming phone numbers, read a number's inbound-message
webhook, and set a number's inbound-message webhook URL. Nothing here is imported
at package import time: each module registers its tool through the global
``tai42_app`` handle and is loaded by the host via the manifest's
``tools[].module`` field.
"""

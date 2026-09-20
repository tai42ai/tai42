"""tai42-tools-whatsapp — WhatsApp provisioning tools for the TAI ecosystem.

An opt-in, manifest-loaded collection of Meta Graph provisioning tools: register a message
template, list the business account's message templates, delete a template by name, and subscribe
the app to a WhatsApp Business Account's webhooks. Nothing here is imported at package import time:
each module registers its tool through the global ``tai42_app`` handle and is loaded by the host via
the manifest's ``tools[].module`` field.
"""

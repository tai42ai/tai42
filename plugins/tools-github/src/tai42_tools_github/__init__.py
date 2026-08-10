"""tai42-tools-github — GitHub provisioning tools for the TAI ecosystem.

An opt-in, manifest-loaded collection of GitHub repository-webhook setup tools: create a
repository webhook, list a repository's webhooks, and delete a repository webhook. These are the
provider-side provisioning counterpart to a runtime signature verifier — they set up the hooks a
verifier later authenticates. Nothing here is imported at package import time: each module registers
its tool through the global ``tai42_app`` handle and is loaded by the host via the manifest's
``tools[].module`` field.
"""

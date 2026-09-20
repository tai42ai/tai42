"""tai42-toolbox — the reference contrib package for the TAI ecosystem.

An opt-in, manifest-loaded collection of generic tools and tool extensions. Nothing here is
imported at package import time: each module registers its tool or tool extension through the
global ``tai42_app`` handle and is loaded by the host via the manifest's ``tools[].module`` /
``extensions_modules`` fields. Heavy backing dependencies are opt-in extras; a module whose
extra is missing fails loudly with an install hint.
"""

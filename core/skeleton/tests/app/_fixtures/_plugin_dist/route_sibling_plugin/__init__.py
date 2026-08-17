"""A standalone top-level fixture package modelling a real route-carrying plugin.

Its distribution layout mirrors ``tai42_channel_twilio``: the manifest leaf
(``register``) registers by importing a route-carrying SIBLING (``inbound``) whose
module body records the plugin's HTTP route. Lives OUTSIDE the ``tests`` package so
the importer's whole-distribution reload can widen to it without pulling the test
suite in — the repro test puts its parent directory on ``sys.path``.
"""

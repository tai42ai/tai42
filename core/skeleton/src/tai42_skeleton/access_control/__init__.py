"""Access control: ASGI auth gate (identity + policy + route guard).

Nothing is re-exported here, so importing a leaf such as
:mod:`tai42_skeleton.access_control.path_canon` loads that leaf alone. The ASGI
gate is :class:`~tai42_skeleton.access_control.adapter.AuthAdapter` (builds the
middleware stack and verifies tokens) and its settings are the cached
:func:`~tai42_skeleton.access_control.settings.access_control_settings`. The
identity, policy and model contracts live in ``tai42_contract.access_control``;
the classes here implement them.
"""

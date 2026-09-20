"""Local commands the server contributes to ``tai`` — no ``/api/*`` counterpart.

The runtime launchers (``serve``/``backend``/``metrics``) are the click commands
in the sibling launcher modules; the modules here carry the database,
diagnostics, catalog, and OpenAPI commands. They are mounted onto the ``tai``
command through :func:`tai42_skeleton.cli.local.register`.
"""

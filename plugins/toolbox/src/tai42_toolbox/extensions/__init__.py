"""Tool extensions — each wraps or transforms a single registered tool.

Every module registers through ``tai42_app.extensions.extension`` with an
``ExtensionKind`` and is loaded by the host via the manifest's
``extensions_modules`` field.
"""

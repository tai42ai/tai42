"""Private helper modules backing the WhatsApp provisioning tools.

Not part of the public API: the module here holds the shared Meta Graph client (message-template
register/list/delete and the app webhook subscribe) that the registered entrypoints in
``tai42_tools_whatsapp.tools`` delegate to. Nothing here registers through ``tai42_app``.
"""

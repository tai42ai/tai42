"""Private helper modules backing the Twilio provisioning tools.

Not part of the public API: the module here holds the shared Twilio REST client
(incoming-number list with paging, one-number read, and the SmsUrl update) that
the registered entrypoints in ``tai42_tools_twilio.tools`` delegate to. Nothing
here registers through ``tai42_app``.
"""

"""Session registrations + transcript streams + pending-question + form records
(plugin-owned Redis).

The store is split by the record family it owns, each submodule reaching Redis
through the shared connection helpers in :mod:`connection`:

* :mod:`connection` — the store's Redis connection (the settings guard is private to
  it) and the record-field primitives every family shares.
* :mod:`registrations` — a session registration is the string key
  ``channel:web:session:{token}`` holding the visitor id the cookie token resolves to
  and the web route identity it was minted on. Only a REGISTERED token is a session:
  an unregistered one resolves to nothing, so an invented or planted cookie value can
  never become a conversation address; and the identity is what binds one session to
  one route — a door refuses a session minted elsewhere exactly as it refuses an
  unknown token. A fresh mint gets only ``session_pending_ttl_seconds``;
  ``resolve_session`` promotes it to the full ``session_ttl_seconds`` (one ``GETEX``),
  so a cookie that comes back never ages out mid-conversation while a mint nobody
  returns with expires in minutes.
* :mod:`entry_gate` — the route entry gate: the explicit gate flag, the hashed
  multi-use entry codes, and the per-client guess throttle.
* :mod:`transcript` — the browser-replay transcript, one Redis STREAM per
  conversation keyed by the pair ``(identity, address)``. Each entry holds one
  ready-to-emit SSE frame: an ``event`` name and a ``data`` field already
  ``json.dumps``'d at write, so replay re-emits it verbatim (the JSON encoding is what
  stops a newline in a message body from injecting extra frames). XADD trims to
  exactly ``transcript_max_entries`` and every append refreshes the key's TTL (one
  pipeline) — the durable record of a turn is the conversation bridge's; this stream
  is a bounded replay buffer only. ``transcript_order`` is the write-order gate: the
  message door holds a conversation's lock from ``accept`` until the visitor's own
  frame is written, and every agent-side append takes it, so a reply the accepted turn
  spawns can never precede the message that caused it.
* :mod:`questions` — a pending-question record, the string key
  ``channel:web:question:{id}`` holding the delivery's ``callback_url`` plus the
  ``(identity, address)`` the ``chat.answered`` frame is later appended to.
  ``reserve_question`` writes it with a TTL of the remaining answer budget (an expired
  question cannot be answered); ``peek_question`` reads it without claiming (the
  answer door checks ownership first); ``claim_question`` claims it with ``GETDEL``
  (atomic — a duplicate answer POST gets ``None``); ``restore_question`` puts it back
  with ``SET NX`` after a failed forward, refused with a loud log rather than
  clobbering a newer reservation, and refused again once the record has already been
  restored ``max_restores`` times.
* :mod:`forms` — an ask-less form's submission record, the string key
  ``channel:web:form:{token}`` holding the transcript pair its card was appended to,
  the form's answer schema, and the prompt message. Unlike a question record it is
  READ, never claimed: a form may be submitted again and again (each submission is its
  own participant message), and its TTL is the transcript TTL — the card ages out of
  the replay buffer and its answerability with it.
"""

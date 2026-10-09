"""The states transactional outbox: a run's validated writes and deferred calls, saved before the reply.

A unit's commit writes ONE ``state_outbox`` row in one small transaction; the row is applied after
the caller returns — the record writes and the row's removal in one Postgres transaction, in
per-subject order — and its deferred calls run after that. Every reader and run entry on a
subject waits for its outstanding saves first; a failed save holds its subjects loudly until an
operator retries or discards it.
"""

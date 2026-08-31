-- The platform-side RUNS INDEX — one enumerable row per run, so a deployment can
-- list its runs WITHOUT the observability vendor. A "run" here is one OUTERMOST
-- registered-preset dispatch (the run_tool chokepoint writes exactly one row per
-- such dispatch, aligning a row with a monitoring trace); nested sub-preset
-- dispatches and raw (non-preset) tool calls are NOT rows. Node-level detail stays
-- in checkpoints and the vendor — this table is enumeration only.
--
-- The row is written at dispatch START (`outcome='running'`, `ended_at` NULL) and
-- updated at completion/exception to its terminal `outcome`. A row left `running`
-- with a NULL `ended_at` is an in-flight OR crash-interrupted run — reported
-- honestly as such, never silently reconciled here (that is the tool-run surface's
-- job). `trace_id` is the monitoring trace for deep-linking; it is NULLABLE and
-- best-effort ("captured once available") — a preset whose body opens no trace, or
-- a deployment with monitoring disabled, simply has none.
--
-- `preset_name`/`preset_version` are the ambient preset identity + retained active
-- version the same chokepoint stamps onto the trace; `user_id`/`session_id` are the
-- generic attribution identity (a conversation's person-or-address and its resolved
-- thread) deposited at the run seam — generic key/values the platform assigns no
-- meaning to beyond "the value a reader filters/groups on". Single-tenant: no tenant
-- column. Idempotent (`IF NOT EXISTS`) so a replay is inert.
--
-- `interaction_id` is the LIFECYCLE-CORRELATION key (nullable): a dispatch that
-- async-parks records the interaction id its `SuspendedInteraction` sentinel carries,
-- and the later out-of-band resume dispatch (the continuation fire) records the SAME
-- id at its START — so one equality query joins a logical run's parked row and its
-- resume row. First-set wins (the store's COALESCE keeps an existing value): a resume
-- that parks AGAIN keeps its ORIGIN id — the new park's id is NOT separately recorded,
-- so the column fully joins a SINGLE-park lifecycle (the dominant case); a multi-park
-- chain is not end-to-end walkable by interaction_id alone (deep-chain reconstruction
-- stays with the checkpoint/vendor story). NULL when no correlation exists (a plain
-- run), never an error — fail-safe like every other write at this chokepoint.
--
-- `outcome='aborted'` is a CANCELLED dispatch (asyncio.CancelledError — a user
-- abort, a shutdown drain, an epoch retire): cancelled is not failed, so it is a
-- distinct terminal state from `error`. The terminal write during a cancellation is
-- best-effort and unshielded — a run whose cancellation outruns the write honestly
-- stays `running` (crash-interrupted posture) rather than blocking the cancel.
CREATE TABLE IF NOT EXISTS run_index (
    run_id         TEXT         NOT NULL,
    preset_name    TEXT         NOT NULL,
    preset_version INTEGER      NOT NULL,
    trace_id       TEXT,
    user_id        TEXT,
    session_id     TEXT,
    interaction_id TEXT,
    outcome        TEXT         NOT NULL DEFAULT 'running',
    started_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ended_at       TIMESTAMPTZ,
    PRIMARY KEY (run_id),
    CONSTRAINT run_index_outcome_check
        CHECK (outcome IN ('running', 'success', 'error', 'parked', 'aborted'))
);

-- The list door pages newest-first over `started_at` and the retention prune deletes
-- by `started_at` cutoff, so a DESC index backs both the ordered page scan and the
-- range delete.
CREATE INDEX IF NOT EXISTS run_index_started_at_idx
    ON run_index (started_at DESC);

-- The remaining indexes back the equality filters the list door exposes (preset
-- name+version, attribution user, attribution session, lifecycle interaction), so a
-- filtered page never scans the whole table.
CREATE INDEX IF NOT EXISTS run_index_preset_idx
    ON run_index (preset_name, preset_version);
CREATE INDEX IF NOT EXISTS run_index_user_idx
    ON run_index (user_id);
CREATE INDEX IF NOT EXISTS run_index_session_idx
    ON run_index (session_id);
CREATE INDEX IF NOT EXISTS run_index_interaction_idx
    ON run_index (interaction_id);

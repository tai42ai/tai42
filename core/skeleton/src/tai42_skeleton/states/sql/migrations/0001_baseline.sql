-- The subject-keyed state store — a door-agnostic record substrate keyed by
-- SUBJECT under the conversation-target scope. A subject is (target_kind,
-- target_name, kind, key):
-- the (target_kind, target_name) pair is the only tenancy this platform has, kind
-- names the subject family a state declares, key addresses one subject in it.
--
-- Seven tables under the `states` component, bound through the kit DB registry
-- (env TAI_DB_BINDING_STATES, defaulting to the `default` database like `skeleton`).
-- The column comments state the invariants the tables cannot express; the store
-- (states/store.py) enforces the rest.

-- A declared state: its author base `schema`, the composed `effective_schema`
-- every document validation reads (base + each mounted module's fragment), the
-- `subject_kinds` it serves and the `default_subject_kind` a door's ambient subject
-- resolves to, and an optional per-state `retention_days` (INT4; NULL keeps records
-- forever unless the global default is set).
CREATE TABLE IF NOT EXISTS state_declarations (
    name                 TEXT PRIMARY KEY,
    description          TEXT        NOT NULL DEFAULT '',
    schema               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    effective_schema     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    subject_kinds        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    default_subject_kind TEXT        NOT NULL DEFAULT '',
    retention_days       INTEGER,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A platform state-module document (schema fragment + parameters + write regimes +
-- mount-time declarations + trace switch). `shipped_hash` is the seed applier's
-- canonical-body hash on a shipped default (NULL for an operator upload); it is the
-- only field that tells an unedited shipped module from an operator-owned one.
CREATE TABLE IF NOT EXISTS state_modules (
    name         TEXT PRIMARY KEY,
    body         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    shipped_hash TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One mount of a module on a state at `path`, carrying the resolved `parameters`
-- and the mount-time `declarations` values. The effective schema on the state is
-- recomposed from every mount in the same transaction as a mount write.
CREATE TABLE IF NOT EXISTS state_mounts (
    state        TEXT        NOT NULL,
    module       TEXT        NOT NULL,
    path         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    parameters   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    declarations JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (state, module)
);
CREATE INDEX IF NOT EXISTS state_mounts_module_idx ON state_mounts (module);

-- One record: the JSON `data` document for a subject, validated whole against the
-- state's effective schema on every write, plus its `updated_at` (the write's
-- commit-ordered clock, returned as `seq` — the channel ordering key). The subject
-- is the four columns; equality (and identity across every store method) is all
-- four under `state`.
CREATE TABLE IF NOT EXISTS state_records (
    state        TEXT             NOT NULL,
    target_kind  TEXT             NOT NULL,
    target_name  TEXT             NOT NULL,
    subject_kind TEXT             NOT NULL,
    subject_key  TEXT             NOT NULL,
    data         JSONB            NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (state, target_kind, target_name, subject_kind, subject_key)
);
CREATE INDEX IF NOT EXISTS state_records_data_gin ON state_records USING gin (data jsonb_path_ops);

-- A subject alias: a fold leaves ONE live record and an alias row, and every record
-- access resolves the subject through this table (one hop by invariant — a fold
-- flattens every alias that pointed at the folded subject onto the new canonical).
-- Aliases are IDENTITY, not data: the retention sweep never touches them; an erase
-- deletes the surviving record AND every alias pointing at it; a declaration delete
-- cascades them.
CREATE TABLE IF NOT EXISTS state_subject_aliases (
    state          TEXT        NOT NULL,
    target_kind    TEXT        NOT NULL,
    target_name    TEXT        NOT NULL,
    alias_kind     TEXT        NOT NULL,
    alias_key      TEXT        NOT NULL,
    canonical_kind TEXT        NOT NULL,
    canonical_key  TEXT        NOT NULL,
    mode           TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (state, target_kind, target_name, alias_kind, alias_key)
);
CREATE INDEX IF NOT EXISTS state_subject_aliases_canonical_idx
    ON state_subject_aliases (state, target_kind, target_name, canonical_kind, canonical_key);

-- The idempotency ledger: an `apply` carrying an op_id inserts here first; a replayed
-- id returns without touching the record. Rows older than the op-retention window are
-- pruned opportunistically on the write that inserts new ones.
CREATE TABLE IF NOT EXISTS state_applied_ops (
    op_id      TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS state_applied_ops_applied_at_idx ON state_applied_ops (applied_at);

-- The write provenance ledger — the audit of who wrote what. One row per write door
-- crossing, recorded in the same transaction as the record change: the completed
-- origin (`door`/`actor` stamped by the platform chokepoint from the ambient context;
-- `consumer`/`meta`/`run_id`/`turn_id`/`op_id` carrying what each side knows) and the
-- absolute `paths` the write touched. The ledger is never optional and a consumer can
-- never forge `door`/`actor`.
CREATE TABLE IF NOT EXISTS state_writes (
    id           BIGSERIAL PRIMARY KEY,
    state        TEXT        NOT NULL,
    target_kind  TEXT        NOT NULL,
    target_name  TEXT        NOT NULL,
    subject_kind TEXT        NOT NULL,
    subject_key  TEXT        NOT NULL,
    seq          DOUBLE PRECISION,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    door         TEXT        NOT NULL,
    actor        TEXT,
    consumer     TEXT,
    meta         JSONB,
    run_id       TEXT,
    turn_id      TEXT,
    paths        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    op_id        TEXT
);
CREATE INDEX IF NOT EXISTS state_writes_subject_idx
    ON state_writes (state, target_kind, target_name, subject_kind, subject_key, id DESC);

-- ============================================================
-- tai42-accounts-postgres — accounts / sessions / invites schema
-- ============================================================
-- The plugin's OWN tables: human user accounts, their session tokens,
-- and their pending invites. Single-tenant by design (no org_id /
-- tenant column). This plugin owns and applies its own schema out-of-band via
-- its own apply step (`python -m tai42_accounts_postgres.db apply`) — it rides no
-- external migration tool. Every statement is IF NOT EXISTS, so re-running the
-- script is a no-op.
-- Database: tai (plugin-owned objects in the platform DB)
-- ============================================================

-- ------------------------------------------------------------
-- Accounts — one row per human user
-- ------------------------------------------------------------
-- `user_id` is the opaque, stable identity (`usr-<token_urlsafe>`) that keys
-- policies, key-ownership claims, and sessions — never the email, which is a
-- mutable contact attribute stored normalized (trim + lower). `password_hash`
-- is NULL until an invite is accepted. `role` is the role-template name applied
-- to the user's enforced policy.
CREATE TABLE IF NOT EXISTS accounts_users (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    email         TEXT NOT NULL,            -- stored normalized (trim + lower)
    password_hash TEXT,                     -- NULL until an invite is accepted
    role          TEXT NOT NULL,            -- role-template name
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT accounts_users_user_id_unique UNIQUE (user_id),
    CONSTRAINT accounts_users_email_unique UNIQUE (email)
);

-- ------------------------------------------------------------
-- Sessions — one row per live login
-- ------------------------------------------------------------
-- Session tokens are `tai-sess-<token_urlsafe>` stored ONLY as their SHA-256
-- hash (the raw token appears once, in the login response). `last_seen_at`
-- drives sliding-idle expiry (updated at most every 60 s); `absolute_expires_at`
-- is the hard cap from mint. Expired rows are garbage-collected opportunistically
-- on session mint.
CREATE TABLE IF NOT EXISTS accounts_sessions (
    id                  BIGSERIAL PRIMARY KEY,
    token_hash          TEXT NOT NULL,      -- sha256(raw); raw never stored
    user_id             TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT accounts_sessions_token_hash_unique UNIQUE (token_hash)
);
CREATE INDEX IF NOT EXISTS accounts_sessions_user_id_idx ON accounts_sessions (user_id);

-- ------------------------------------------------------------
-- Invites — one pending invite per user
-- ------------------------------------------------------------
-- Invite tokens are `tai-inv-<token_urlsafe>` stored ONLY as their SHA-256 hash.
-- Consumption is atomic (a single UPDATE sets `consumed_at` under a TTL guard),
-- so an invite is single-use by row semantics, not by read-then-write. Expired /
-- consumed rows are garbage-collected opportunistically on invite creation.
CREATE TABLE IF NOT EXISTS accounts_invites (
    id          BIGSERIAL PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    CONSTRAINT accounts_invites_token_hash_unique UNIQUE (token_hash)
);
CREATE INDEX IF NOT EXISTS accounts_invites_user_id_idx ON accounts_invites (user_id);

-- Claude Agora coordination hub schema (PostgreSQL). See README.md for
-- the full design reasoning (group vs thread, roles vs sessions).
--
-- Group vs thread (added 2026-09-17): a GROUP is a durable circle of
-- member sessions, reused across many topics. A THREAD is a group of
-- messages about one specific topic, and belongs to exactly one group.
-- Membership lives on the group; topic and message history live on the
-- thread. This file is what a FRESH database gets (docker-entrypoint-
-- initdb.d only runs on an empty volume) -- an already-running database
-- picks up the equivalent shape via the idempotent migration in
-- api/main.py's startup handler instead.

CREATE TABLE IF NOT EXISTS roles (
    role        TEXT PRIMARY KEY,
    repo_path   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- When this role last used the API as itself (bind, post, read, ack,
    -- pending pull), and what it did. For the dashboard's Roles panel.
    last_seen_at     TIMESTAMPTZ,
    last_seen_action TEXT,
    last_action_at   TIMESTAMPTZ,
    -- mailwatch.sh heartbeat: a watch is running while heartbeats keep
    -- arriving within ~3 intervals.
    heartbeat_at       TIMESTAMPTZ,
    heartbeat_interval INTEGER
);

CREATE TABLE IF NOT EXISTS role_bindings (
    role            TEXT PRIMARY KEY REFERENCES roles(role),
    session_name    TEXT,
    bound_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'live'
);

CREATE TABLE IF NOT EXISTS groups (
    group_id     SERIAL PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_from TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id   INTEGER NOT NULL REFERENCES groups(group_id),
    role       TEXT NOT NULL REFERENCES roles(role),
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at    TIMESTAMPTZ,
    PRIMARY KEY (group_id, role)
);

CREATE TABLE IF NOT EXISTS threads (
    thread_id    SERIAL PRIMARY KEY,
    group_id     INTEGER NOT NULL REFERENCES groups(group_id),
    topic        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_from TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id   SERIAL PRIMARY KEY,
    thread_id    INTEGER NOT NULL REFERENCES threads(thread_id),
    sender_role  TEXT NOT NULL REFERENCES roles(role),
    body         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- needs_operator flag (2026-09-25): kind is decision|approval|security|bug|other,
    -- why is one short line. Answered by the operator's reply, or cleared without one.
    attn_kind        TEXT,
    attn_why         TEXT,
    attn_answered_by INTEGER REFERENCES messages(message_id),
    attn_cleared_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS deliveries (
    message_id     INTEGER NOT NULL REFERENCES messages(message_id),
    recipient_role TEXT NOT NULL REFERENCES roles(role),
    delivered_at   TIMESTAMPTZ,
    read_at        TIMESTAMPTZ,
    -- The "thumbs up": info-only, no reply needed. Distinct from read_at.
    ack_at         TIMESTAMPTZ,
    PRIMARY KEY (message_id, recipient_role)
);

CREATE INDEX IF NOT EXISTS messages_attn_idx ON messages (message_id) WHERE attn_kind IS NOT NULL;

-- Sender identity for the operator's dashboard posts only. The operator
-- isn't a role: they are never a group member, never bind and are never a
-- recipient.
INSERT INTO roles (role) VALUES ('operator') ON CONFLICT DO NOTHING;

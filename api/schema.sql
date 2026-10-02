-- Claude Agora coordination hub schema (MariaDB 10.5+).
-- See README.md for the full design reasoning (group vs thread, roles vs
-- sessions).
--
-- Group vs thread (added 2026-09-17): a GROUP is a durable circle of member
-- sessions, reused across many topics. A THREAD is a group of messages about
-- one specific topic, and belongs to exactly one group. Membership lives on
-- the group; topic and message history live on the thread.
--
-- The API applies this file itself on every startup (api/agora_db.py
-- apply_schema), so every statement must stay idempotent, and add any later
-- schema change here as an idempotent ALTER (ADD COLUMN IF NOT EXISTS ...).
-- Keep statements separated by a semicolon and comments on their own lines.
--
-- Conventions: timestamps are DATETIME(6) holding UTC (no TIMESTAMP: no 2038
-- limit, no implicit ON UPDATE, no time-zone conversion). Text is utf8mb4 with
-- the binary NO PAD collation, so comparisons are case-sensitive and
-- trailing-space-exact, as they were on PostgreSQL. Every table has a primary
-- key and is InnoDB.

CREATE TABLE IF NOT EXISTS roles (
    role        VARCHAR(100) NOT NULL PRIMARY KEY,
    repo_path   TEXT,
    created_at  DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    -- When this role last used the API as itself (bind, post, read, ack,
    -- pending pull), and what it did. For the dashboard's Roles panel.
    last_seen_at     DATETIME(6) NULL,
    last_seen_action TEXT,
    last_action_at   DATETIME(6) NULL,
    -- mailwatch.sh heartbeat: a watch is running while heartbeats keep
    -- arriving within ~3 intervals.
    heartbeat_at       DATETIME(6) NULL,
    heartbeat_interval INT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

CREATE TABLE IF NOT EXISTS role_bindings (
    role            VARCHAR(100) NOT NULL PRIMARY KEY,
    session_name    VARCHAR(200),
    bound_at        DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    status          VARCHAR(20) NOT NULL DEFAULT 'live',
    FOREIGN KEY (role) REFERENCES roles(role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

CREATE TABLE IF NOT EXISTS `groups` (
    group_id     INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    created_from TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

-- One row per distinct member set ever passed to POST /groups: the row lock
-- that serialises concurrent get-or-create calls for the same set.
CREATE TABLE IF NOT EXISTS group_locks (
    name CHAR(32) NOT NULL PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

CREATE TABLE IF NOT EXISTS group_members (
    group_id   INT NOT NULL,
    role       VARCHAR(100) NOT NULL,
    joined_at  DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    left_at    DATETIME(6) NULL,
    PRIMARY KEY (group_id, role),
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id),
    FOREIGN KEY (role) REFERENCES roles(role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

CREATE TABLE IF NOT EXISTS threads (
    thread_id    INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    group_id     INT NOT NULL,
    topic        TEXT NOT NULL,
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    created_from TEXT,
    FOREIGN KEY (group_id) REFERENCES `groups`(group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

CREATE TABLE IF NOT EXISTS messages (
    message_id   INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    thread_id    INT NOT NULL,
    sender_role  VARCHAR(100) NOT NULL,
    body         MEDIUMTEXT NOT NULL,
    created_at   DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    -- needs_operator flag (2026-09-25): kind is decision|approval|security|bug|other,
    -- why is one short line. Answered by the operator's reply, or cleared without one.
    attn_kind        VARCHAR(20),
    attn_why         TEXT,
    attn_answered_by INT,
    attn_cleared_at  DATETIME(6) NULL,
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id),
    FOREIGN KEY (sender_role) REFERENCES roles(role),
    FOREIGN KEY (attn_answered_by) REFERENCES messages(message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

CREATE TABLE IF NOT EXISTS deliveries (
    message_id     INT NOT NULL,
    recipient_role VARCHAR(100) NOT NULL,
    delivered_at   DATETIME(6) NULL,
    read_at        DATETIME(6) NULL,
    -- The "thumbs up": info-only, no reply needed. Distinct from read_at.
    ack_at         DATETIME(6) NULL,
    PRIMARY KEY (message_id, recipient_role),
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    FOREIGN KEY (recipient_role) REFERENCES roles(role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

-- Scheduled jobs run by the API itself (the weekly stale-thread sweep). A
-- run claims its interval by updating last_run_at, so only one replica runs it.
CREATE TABLE IF NOT EXISTS job_runs (
    job         VARCHAR(100) NOT NULL PRIMARY KEY,
    last_run_at DATETIME(6) NULL,
    last_result TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_nopad_bin;

-- MariaDB has no partial indexes; (attn_kind, message_id) serves the
-- "attn_kind IS NOT NULL" scans the same way.
CREATE INDEX IF NOT EXISTS messages_attn_idx ON messages (attn_kind, message_id);

-- Sender identity for the operator's dashboard posts only. The operator
-- isn't a role: they are never a group member, never bind and are never a
-- recipient.
INSERT IGNORE INTO roles (role) VALUES ('operator');

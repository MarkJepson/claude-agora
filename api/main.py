"""Claude Agora backing API: a small REST API + MariaDB database that lets many
independent Claude Code sessions coordinate through durable groups and
threads, instead of ad hoc peer-to-peer messages.

Group vs thread (added 2026-09-17): a GROUP is a durable circle of member
sessions, reused across many topics over time. A THREAD is a group of
messages about one specific topic, and belongs to exactly one group.
Membership lives on the group; topic and message history live on the
thread.
"""
import hashlib
import hmac
import html
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

import agora_db
from agora_db import ph, retry_on_db_error
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

DATABASE_URL = os.environ["DATABASE_URL"]
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
STATIC_DIR = Path(__file__).parent / "static"
# The operator isn't a role (design decision, 2026-09-26): they see every
# group, thread and message through the dashboard, so the operator is never
# a group member, never binds and is never a recipient. 'operator' exists in
# the roles table only as the sender_role of their dashboard posts
# (messages.sender_role needs it).
OPERATOR = "operator"

log = logging.getLogger("uvicorn.error")


def _reject_operator(role: str, what: str) -> None:
    if role == OPERATOR:
        raise HTTPException(422, f"'{OPERATOR}' can't {what}: the operator isn't a role; they see every thread on the dashboard")

app = FastAPI(title="Claude Agora relay API")
pool: Optional[agora_db.Pool] = None

# --- CSRF hardening ---
# The relay has no auth and trusts any sender_role on localhost (documented
# posture), but nothing stopped a THIRD PARTY -- a web page open in the
# operator's browser -- from reaching it too: no Origin/Host check, and FastAPI parses
# a POST with no Content-Type as JSON, so a "simple" cross-origin request
# (no CORS preflight) got through. Three checks, applied to every
# state-changing request (POST/PUT/PATCH/DELETE):
#   1. Host must be one this relay is actually served as -- blocks DNS
#      rebinding reaching it under a different hostname.
#   2. Origin, if the caller sent one, must match. Non-browser callers
#      (curl, the relay client) don't send Origin at all, so they're
#      unaffected; a browser can't be made to lie about its own Origin.
#   3. A custom header (X-Relay-Client) must be present. A browser can't
#      set this on a cross-origin request without a CORS preflight, and
#      this relay never answers one with an Access-Control-Allow-Origin,
#      so the preflight -- and the real request -- fails closed. This is
#      what actually closes the no-Content-Type loophole: it doesn't
#      matter how the body is parsed if the request never arrives.
# GETs are exempt from checks 2/3 (nothing to protect -- there's no body,
# and the Host check alone stops DNS rebinding from reading /pending or
# /api/dashboard/state). scripts/relay and the dashboard's JS both send
# X-Relay-Client; anything still hand-writing curl per the old protocol
# examples needs to add it too.
_ALLOWED_HOSTS = {h.strip() for h in os.environ.get("RELAY_ALLOWED_HOSTS", "127.0.0.1:8089,localhost:8089").split(",") if h.strip()}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_HEADER = "x-relay-client"


@app.middleware("http")
async def _csrf_guard(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": f"unrecognized Host {host!r}"}, status_code=400)
    if request.method in _MUTATING_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and origin not in _ALLOWED_ORIGINS:
            return JSONResponse({"detail": f"unrecognized Origin {origin!r}"}, status_code=403)
        if _CSRF_HEADER not in request.headers:
            return JSONResponse(
                {"detail": f"missing required {_CSRF_HEADER!r} header on a state-changing request"},
                status_code=403,
            )
    return await call_next(request)


# --- Operator-only auth (2026-09-26 design; approved 2026-09-28) ---
# Only the dashboard should be able to post as sender_role="operator". The
# dashboard holds a shared secret (entered once by the operator in their
# browser, kept in sessionStorage only -- never written to disk by any
# session or by this relay) and sends it as X-Operator-Token on every
# mutating request. OPERATOR_TOKEN is an env var the operator sets
# themselves (e.g. in a gitignored .env docker-compose reads); it's never
# generated or written by this code. Soft rollout: if OPERATOR_TOKEN isn't
# set, operator-posting is unchanged from before (a warning is logged once
# at startup) -- this only starts enforcing once the operator actually
# configures a token.
# Surrounding whitespace is ignored: a token Secret made from a file usually
# carries a trailing newline, which would otherwise never match what the
# operator types into the dashboard. Blank counts as not set.
_OPERATOR_TOKEN = (os.environ.get("OPERATOR_TOKEN") or "").strip() or None
# Opt-in strictness for deployments where "unrestricted" must never happen by
# accident: with REQUIRE_OPERATOR_TOKEN=true the relay refuses to start unless
# OPERATOR_TOKEN is non-blank. (Off by default so a fresh checkout still works
# without a token; the warning below covers that case.)
_REQUIRE_OPERATOR_TOKEN = os.environ.get("REQUIRE_OPERATOR_TOKEN", "").strip().lower() in ("1", "true", "yes")


def _check_operator_token_config() -> None:
    if _REQUIRE_OPERATOR_TOKEN and not _OPERATOR_TOKEN:
        raise RuntimeError(
            "REQUIRE_OPERATOR_TOKEN is set but OPERATOR_TOKEN is empty or missing; refusing to start, "
            "because operator posts would be unrestricted."
        )


def _check_operator_token(request: Request, what: str = "this") -> None:
    if not _OPERATOR_TOKEN:
        return
    supplied = request.headers.get("x-operator-token", "")
    # Compare bytes: compare_digest raises TypeError on non-ASCII str (a 500), and
    # a wrong token of any kind must be a plain 403.
    if not hmac.compare_digest(supplied.encode(), _OPERATOR_TOKEN.encode()):
        raise HTTPException(403, f"{what} requires a valid X-Operator-Token header")


async def _touch(conn: agora_db.Conn, role: str, action: str) -> None:
    """Record that `role` itself just used the API. Only for calls a role
    makes as itself -- not delivered/delivered-batch, which coordinator-claude
    makes about other roles, and not GET /roles/{role}, a lookup anyone
    can do."""
    await conn.execute(
        "UPDATE roles SET last_seen_at = $1, last_action_at = $1, last_seen_action = $2 WHERE role = $3",
        _now(), action, role,
    )


async def _require_roles_exist(conn: agora_db.Conn, roles: list[str]) -> None:
    """Roles are FK-referenced everywhere (role_bindings, group_members,
    messages.sender_role); without this check an unregistered role hits a
    raw ForeignKeyViolationError and surfaces as a 500 instead of a 404."""
    rows = await conn.fetch(f"SELECT role FROM roles WHERE role IN ({ph(1, len(roles))})", *roles)
    known = {r["role"] for r in rows}
    missing = [r for r in roles if r not in known]
    if missing:
        raise HTTPException(404, f"role(s) not registered: {', '.join(missing)}")


@app.on_event("startup")
async def startup() -> None:
    global pool
    _check_operator_token_config()
    if _OPERATOR_TOKEN and len(_OPERATOR_TOKEN) < 32:
        log.warning(
            "OPERATOR_TOKEN is only %d characters; use a long random value (e.g. `openssl rand -hex 32`).",
            len(_OPERATOR_TOKEN),
        )
    if not _OPERATOR_TOKEN:
        log.warning(
            "OPERATOR_TOKEN not set -- sender_role='operator' is NOT restricted to the dashboard yet. "
            "Set OPERATOR_TOKEN (e.g. in a gitignored .env) and configure it in the dashboard to enforce this."
        )
    pool = await agora_db.create_pool(DATABASE_URL, min_size=1, max_size=int(os.environ.get("DATABASE_POOL_MAX", "4")))
    async with pool.acquire() as conn:
        await agora_db.apply_schema(conn, SCHEMA_PATH)


@app.on_event("shutdown")
async def shutdown() -> None:
    if pool:
        await pool.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BindRequest(BaseModel):
    session_name: str = Field(max_length=200)


class CreateGroupRequest(BaseModel):
    members: list[str]


class CreateThreadRequest(BaseModel):
    topic: str = Field(max_length=300)


class AddMemberRequest(BaseModel):
    role: str


AttentionKind = Literal["decision", "approval", "security", "bug", "other"]


class NeedsOperator(BaseModel):
    kind: AttentionKind
    why: str = Field(min_length=1, max_length=200)


class LogMessageRequest(BaseModel):
    sender_role: str
    body: str = Field(max_length=50000)
    # Optional: ask for the operator's attention. `why` is one short line saying
    # what they need to do (decide X, approve PR #N, look at finding Y).
    needs_operator: Optional[NeedsOperator] = None
    # Optional, used by the operator's replies: the message_id (same thread) whose
    # needs_operator this answers.
    answers: Optional[int] = None


class DeliveredBatchRequest(BaseModel):
    roles: list[str]


@app.post("/roles/{role}/bind")
@retry_on_db_error
async def bind_role(role: str, req: BindRequest):
    """One live session per repo: if this session_name is currently the
    live binding for some OTHER role, that binding is superseded and
    marked stale as part of this same call -- a session can't represent
    two repos at once. This is the only path to 'stale' now; there is no
    separate endpoint for it, since the only thing that should ever
    invalidate a binding is another binding taking its place."""
    _reject_operator(role, "be bound")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_roles_exist(conn, [role])
            await conn.execute(
                """UPDATE role_bindings SET status = 'stale'
                   WHERE session_name = $1 AND role != $2 AND status = 'live'""",
                req.session_name, role,
            )
            await conn.execute(
                """INSERT INTO role_bindings (role, session_name, bound_at, status)
                   VALUES ($1, $2, $3, 'live')
                   ON DUPLICATE KEY UPDATE
                     session_name = VALUES(session_name),
                     bound_at = VALUES(bound_at),
                     status = 'live'""",
                role, req.session_name, _now(),
            )
            await _touch(conn, role, "registered " + req.session_name)
    return {"ok": True}


@app.post("/roles/{role}/heartbeat")
@retry_on_db_error
async def heartbeat(role: str, interval: int = 20):
    """Sent by mailwatch.sh on every loop. Marks the role as seen and
    records the watch's poll interval, so the dashboard can flag a watch
    that has stopped (no heartbeat for 3 intervals). Doesn't change the
    role's last action. 404 if the role has never been bound."""
    if not 1 <= interval <= 3600:
        raise HTTPException(422, "interval must be 1..3600 seconds")
    async with pool.acquire() as conn:
        bound = await conn.fetchval("SELECT 1 FROM role_bindings WHERE role = $1", role)
        if not bound:
            raise HTTPException(404, f"role {role} has never been bound")
        now = _now()
        await conn.execute(
            """UPDATE roles SET heartbeat_at = $1, heartbeat_interval = $2, last_seen_at = $1
               WHERE role = $3""",
            now, interval, role,
        )
    return {"ok": True}


@app.get("/roles/{role}")
async def get_role_binding(role: str):
    """Look up who is CURRENTLY authoritative for a role, rather than
    guessing from ListAgents or trusting a session's own prose claim about
    its identity. Added 2026-09-17 after a live mix-up: two sessions both
    showed as 'coordinator-claude' in ListAgents at once (one stale, one live --
    ListAgents doesn't distinguish that), and a peer couldn't tell which
    to trust. This is the source of truth: whichever session_name is
    'live' here is the one bind was most recently called from."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role, session_name, status, bound_at FROM role_bindings WHERE role = $1",
            role,
        )
    if row is None:
        raise HTTPException(404, f"role {role} has never been bound")
    return dict(row)


@app.post("/groups")
@retry_on_db_error
async def create_group(req: CreateGroupRequest):
    """Get or create the group for exactly this set of members. A group is
    a durable circle of member sessions with no topic of its own; if an
    active group already has exactly these current members, that one is
    returned with existing=true instead of creating a duplicate (design decision,
    2026-09-26). Add a thread to it rather than making a new circle."""
    members = sorted(set(req.members))
    if OPERATOR in members:
        _reject_operator(OPERATOR, "be a group member")
    if not members:
        raise HTTPException(422, "members must not be empty")
    async with pool.acquire() as conn:
        # Serialise concurrent requests for the same member set, so two
        # callers can't both miss the lookup and create twins: an exclusive
        # row lock per member set (not GET_LOCK, which is not shared between
        # nodes of a replicated database). The row is created OUTSIDE the transaction: doing the
        # INSERT IGNORE inside it takes a shared lock that the FOR UPDATE
        # then upgrades, and racing callers deadlock.
        lock_name = hashlib.md5(",".join(members).encode()).hexdigest()
        await conn.execute("INSERT IGNORE INTO group_locks (name) VALUES ($1)", lock_name)
        async with conn.transaction():
            await conn.fetchval("SELECT name FROM group_locks WHERE name = $1 FOR UPDATE", lock_name)
            await _require_roles_exist(conn, members)
            # Exactly these current members: as many active members as the
            # request has, and all of them from the request.
            existing = await conn.fetchval(
                f"""SELECT g.group_id FROM `groups` g
                   JOIN group_members gm ON gm.group_id = g.group_id AND gm.left_at IS NULL
                   WHERE g.status = 'active'
                   GROUP BY g.group_id
                   HAVING COUNT(*) = $1 AND SUM(gm.role IN ({ph(2, len(members))})) = $1
                   ORDER BY g.group_id LIMIT 1""",
                len(members), *members,
            )
            if existing is not None:
                return {"group_id": existing, "existing": True}
            group_id = await conn.insert("INSERT INTO `groups` (created_from) VALUES (NULL)")
            for role in members:
                await conn.execute(
                    "INSERT INTO group_members (group_id, role) VALUES ($1, $2)",
                    group_id, role,
                )
    return {"group_id": group_id, "existing": False}


@app.delete("/groups/{group_id}")
@retry_on_db_error
async def delete_group(group_id: int):
    """Hard delete a group and everything under it (threads, messages,
    deliveries, membership) -- for cleaning up test/accidental groups
    (e.g. dashboard smoke-testing). Not part of the normal lifecycle:
    archiving a thread is the everyday tool; this is for removing a group
    that should never have existed at all."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM `groups` WHERE group_id = $1", group_id)
            if not exists:
                raise HTTPException(404, f"group {group_id} does not exist")
            await conn.execute(
                """DELETE FROM deliveries WHERE message_id IN (
                       SELECT m.message_id FROM messages m
                       JOIN threads t ON t.thread_id = m.thread_id
                       WHERE t.group_id = $1)""",
                group_id,
            )
            # attn_answered_by points at a message in the same thread; InnoDB
            # checks foreign keys row by row, so unlink before deleting.
            await conn.execute(
                """UPDATE messages SET attn_answered_by = NULL WHERE thread_id IN (
                       SELECT thread_id FROM threads WHERE group_id = $1)""",
                group_id,
            )
            await conn.execute(
                """DELETE FROM messages WHERE thread_id IN (
                       SELECT thread_id FROM threads WHERE group_id = $1)""",
                group_id,
            )
            await conn.execute("DELETE FROM threads WHERE group_id = $1", group_id)
            await conn.execute("DELETE FROM group_members WHERE group_id = $1", group_id)
            await conn.execute("DELETE FROM `groups` WHERE group_id = $1", group_id)
    return {"ok": True}


@app.get("/roles/{role}/groups")
async def role_groups(role: str):
    """The role's current groups, each with its ACTIVE threads: the one call
    that replaces sessions walking /groups/{id}/members and /threads/{id}
    to build their hub_groups.md memory file. Read-only. Groups the role has
    left (left_at set) and archived threads are left out; a group with no
    active threads is still listed, so the role knows it is a member."""
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM roles WHERE role = $1", role):
            raise HTTPException(404, f"role {role} does not exist")
        rows = await conn.fetch(
            "SELECT g.group_id, t.thread_id, t.topic "
            "FROM group_members m JOIN `groups` g ON g.group_id = m.group_id "
            "LEFT JOIN threads t ON t.group_id = g.group_id AND t.status = 'active' "
            "WHERE m.role = $1 AND m.left_at IS NULL AND g.status = 'active' "
            "ORDER BY g.group_id, t.thread_id",
            role,
        )
    groups: dict[int, dict] = {}
    for r in rows:
        g = groups.setdefault(r["group_id"], {"group_id": r["group_id"], "threads": []})
        if r["thread_id"] is not None:
            g["threads"].append({"thread_id": r["thread_id"], "topic": r["topic"]})
    return {"role": role, "groups": list(groups.values())}


@app.get("/groups/{group_id}/members")
async def group_members(group_id: int):
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM `groups` WHERE group_id = $1", group_id)
        if not exists:
            raise HTTPException(404, f"group {group_id} does not exist")
        rows = await conn.fetch(
            "SELECT role FROM group_members WHERE group_id = $1 AND left_at IS NULL",
            group_id,
        )
    return {"members": [r["role"] for r in rows]}


@app.post("/groups/{group_id}/members")
@retry_on_db_error
async def add_member(group_id: int, req: AddMemberRequest):
    _reject_operator(req.role, "be a group member")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_roles_exist(conn, [req.role])
            exists = await conn.fetchval("SELECT 1 FROM `groups` WHERE group_id = $1", group_id)
            if not exists:
                raise HTTPException(404, f"group {group_id} does not exist")
            # ON DUPLICATE KEY covers both a genuine re-add (no-op, already a
            # member) and rejoining after having left (left_at is reset).
            await conn.execute(
                """INSERT INTO group_members (group_id, role) VALUES ($1, $2)
                   ON DUPLICATE KEY UPDATE left_at = NULL""",
                group_id, req.role,
            )
    return {"ok": True}


@app.post("/groups/{group_id}/threads")
@retry_on_db_error
async def create_thread(group_id: int, req: CreateThreadRequest):
    """Start a new topic within an existing group -- mechanical, not a
    judgment call: the membership circle is already decided."""
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM `groups` WHERE group_id = $1", group_id)
        if not exists:
            raise HTTPException(404, f"group {group_id} does not exist")
        thread_id = await conn.insert(
            "INSERT INTO threads (group_id, topic) VALUES ($1, $2)",
            group_id, req.topic,
        )
    return {"thread_id": thread_id}


@app.get("/groups/{group_id}/threads/lookup")
async def find_thread_by_topic(group_id: int, topic: str):
    """Topic lookup is scoped to one group, since a thread belongs to
    exactly one group -- avoids collisions between unrelated circles that
    happen to pick similar topic names."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT thread_id FROM threads WHERE group_id = $1 AND topic = $2
               ORDER BY (status = 'active') DESC, created_at DESC LIMIT 1""",
            group_id, topic,
        )
    return {"thread_id": row["thread_id"] if row else None}


@app.post("/threads/{thread_id}/archive")
@retry_on_db_error
async def archive_thread(thread_id: int):
    """Archiving is coordinator-claude's judgment call (same footing as creating
    a group/thread in the first place) -- once a topic is resolved, the
    thread is closed to new messages. Does not delete history."""
    async with pool.acquire() as conn:
        matched = await conn.execute(
            "UPDATE threads SET status = 'archived' WHERE thread_id = $1", thread_id,
        )
    if matched == 0:
        raise HTTPException(404, f"thread {thread_id} does not exist")
    return {"ok": True}


@app.get("/threads/{thread_id}")
async def get_thread_info(thread_id: int):
    """Resolves a bare thread_id back to its group_id/topic/status --
    needed when a role pings coordinator-claude with just a thread_id (it may
    not carry the group_id along), so the group's member list can be
    looked up before fanning out."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT thread_id, group_id, topic, status FROM threads WHERE thread_id = $1",
            thread_id,
        )
    if row is None:
        raise HTTPException(404, f"thread {thread_id} does not exist")
    return dict(row)


@app.post("/threads/{thread_id}/messages")
@retry_on_db_error
async def log_message(thread_id: int, req: LogMessageRequest, request: Request):
    if req.sender_role == OPERATOR:
        _check_operator_token(request, "posting as 'operator'")
    if req.answers is not None and req.sender_role != OPERATOR:
        # Only the operator resolves a needs_operator ask via `answers`
        # (see the needs_operator docstring below) -- otherwise any sender
        # could silently clear another role's open attention flag.
        raise HTTPException(403, "only the operator can use 'answers' to resolve a message")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_roles_exist(conn, [req.sender_role])
            thread_row = await conn.fetchrow(
                "SELECT group_id, status FROM threads WHERE thread_id = $1", thread_id,
            )
            if thread_row is None:
                raise HTTPException(404, f"thread {thread_id} does not exist")
            if thread_row["status"] == "archived":
                raise HTTPException(409, f"thread {thread_id} is archived -- no new messages")
            group_id = thread_row["group_id"]
            members = [
                r["role"] for r in await conn.fetch(
                    "SELECT role FROM group_members WHERE group_id = $1 AND left_at IS NULL",
                    group_id,
                )
            ]
            if not members:
                raise HTTPException(404, f"thread {thread_id}'s group has no members")
            if req.sender_role != OPERATOR and req.sender_role not in members:
                # Peers are trusted, but this is a cheap check (the
                # operator posts into any thread without being a member,
                # by design -- see OPERATOR above).
                raise HTTPException(403, f"{req.sender_role} is not a member of thread {thread_id}'s group")
            if req.answers is not None:
                target_thread = await conn.fetchval(
                    "SELECT thread_id FROM messages WHERE message_id = $1", req.answers,
                )
                if target_thread != thread_id:
                    raise HTTPException(422, f"answers={req.answers} is not a message in thread {thread_id}")
            message_id = await conn.insert(
                """INSERT INTO messages (thread_id, sender_role, body, attn_kind, attn_why)
                   VALUES ($1, $2, $3, $4, $5)""",
                thread_id, req.sender_role, req.body,
                req.needs_operator.kind if req.needs_operator else None,
                req.needs_operator.why if req.needs_operator else None,
            )
            await _touch(conn, req.sender_role, f"posted #{message_id}")
            if req.answers is not None:
                await conn.execute(
                    """UPDATE messages SET attn_answered_by = $1
                       WHERE message_id = $2 AND attn_kind IS NOT NULL AND attn_answered_by IS NULL""",
                    message_id, req.answers,
                )
            for role in members:
                await conn.execute(
                    "INSERT INTO deliveries (message_id, recipient_role, delivered_at) VALUES ($1, $2, NULL)",
                    message_id, role,
                )
    recipients = [r for r in members if r != req.sender_role]
    return {"message_id": message_id, "group_id": group_id, "recipients": recipients}


@app.post("/messages/{message_id}/attention/clear")
@retry_on_db_error
async def clear_attention(message_id: int, request: Request):
    """The operator handled a needs_operator ask without posting a reply (e.g. they
    answered in a session directly). Same operator-only gate as `answers`
    (log_message) -- otherwise any sender could silently clear another
    role's open attention flag."""
    _check_operator_token(request, "clearing an attention flag")
    async with pool.acquire() as conn:
        cleared = await conn.execute(
            """UPDATE messages SET attn_cleared_at = $1
               WHERE message_id = $2 AND attn_kind IS NOT NULL AND attn_cleared_at IS NULL""",
            _now(), message_id,
        )
    if cleared == 0:
        raise HTTPException(404, f"message {message_id} has no open needs_operator flag")
    return {"ok": True}


@app.get("/attention")
async def open_attention():
    """Open needs_operator asks: not answered and not cleared, oldest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT m.message_id, m.thread_id, t.group_id, m.sender_role, m.attn_kind, m.attn_why, m.created_at
               FROM messages m JOIN threads t ON t.thread_id = m.thread_id
               WHERE m.attn_kind IS NOT NULL AND m.attn_answered_by IS NULL AND m.attn_cleared_at IS NULL
               ORDER BY m.message_id"""
        )
    return {"open": [dict(r) for r in rows]}


@app.post("/messages/{message_id}/delivered")
@retry_on_db_error
async def mark_delivered(message_id: int, role: str):
    """WhatsApp-style "delivered" (design decision, 2026-09-26): the message reached the
    session, because its mail watch picked it up. mailwatch.sh calls this
    when it wakes a session. "Read" is separate, and only the session sets
    it. The first delivery time is kept."""
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM messages WHERE message_id = $1", message_id)
        if not exists:
            raise HTTPException(404, f"message {message_id} does not exist")
        await conn.execute(
            """UPDATE deliveries SET delivered_at = $1
               WHERE message_id = $2 AND recipient_role = $3 AND delivered_at IS NULL""",
            _now(), message_id, role,
        )
    return {"ok": True}


@app.post("/messages/{message_id}/delivered-batch")
@retry_on_db_error
async def mark_delivered_batch(message_id: int, req: DeliveredBatchRequest):
    """Mark several recipients of one message delivered in a single call
    (coordinator-claude's fan-out nudges). Same meaning as /delivered: the
    message reached that session; the first delivery time is kept."""
    async with pool.acquire() as conn:
        if req.roles:
            await conn.execute(
                f"""UPDATE deliveries SET delivered_at = $1
                   WHERE message_id = $2 AND recipient_role IN ({ph(3, len(req.roles))}) AND delivered_at IS NULL""",
                _now(), message_id, *req.roles,
            )
    return {"ok": True, "marked": req.roles}


@app.post("/messages/{message_id}/read")
@retry_on_db_error
async def mark_read(message_id: int, role: str):
    """The 'calling off' step: a session works through its GET /pending
    list in order and calls this once per message as it processes each
    one. Deliberately per-message, not bulk -- read_at should mean 'this
    session actually processed this one message', not 'this session
    fetched a list that happened to include it'.

    read_at is immutable once set (design decision, 2026-09-25): a repeat read 409s
    with the original timestamp rather than overwriting it. Overwriting
    hid an earlier session's read of msg 448 and made a correct empty
    /pending look like a dropped delivery."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                """UPDATE deliveries SET read_at = $1
                   WHERE message_id = $2 AND recipient_role = $3 AND read_at IS NULL""",
                _now(), message_id, role,
            )
            if updated == 0:
                row = await conn.fetchrow(
                    "SELECT read_at FROM deliveries WHERE message_id = $1 AND recipient_role = $2",
                    message_id, role,
                )
                if row is None:
                    raise HTTPException(404, f"{role} is not a recipient of message {message_id}")
                read_at = row["read_at"]
                if read_at is not None:
                    raise HTTPException(
                        409, f"already marked as read by you at {read_at.isoformat()}"
                    )
            await _touch(conn, role, f"read #{message_id}")
    return {"ok": True}


@app.post("/messages/{message_id}/ack")
@retry_on_db_error
async def ack_message(message_id: int, role: str):
    """The 'thumbs up': an info-only message needs no reply, just a
    signal that this recipient saw it and is deliberately taking no
    action -- distinct from posting a full reply message. Sets read_at
    too (acking implies processed), so an acked message also drops out of
    /pending. Visible to others via /threads/{id}/history's acked_by.

    ack_at is immutable once set, same as read_at (consistency fix,
    2026-09-29): a repeat ack 409s with the original timestamp rather
    than silently overwriting it, instead of the two behaving differently
    for what's semantically the same "you actioned this once" signal."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.execute(
                """UPDATE deliveries SET ack_at = $1, read_at = COALESCE(read_at, $1)
                   WHERE message_id = $2 AND recipient_role = $3 AND ack_at IS NULL""",
                _now(), message_id, role,
            )
            if updated == 0:
                row = await conn.fetchrow(
                    "SELECT ack_at FROM deliveries WHERE message_id = $1 AND recipient_role = $2",
                    message_id, role,
                )
                if row is None:
                    raise HTTPException(404, f"{role} is not a recipient of message {message_id}")
                ack_at = row["ack_at"]
                if ack_at is not None:
                    raise HTTPException(
                        409, f"already acked by you at {ack_at.isoformat()}"
                    )
            await _touch(conn, role, f"acked #{message_id}")
    return {"ok": True}


@app.get("/roles/{role}/pending")
@retry_on_db_error
async def pending_for_role(role: str, after: int = 0, peek: bool = False):
    """Read-only listing -- does NOT mark anything read. Its one side
    effect is recording the role as last seen (checked mail), because a
    session normally calls this for itself. Pass peek=true when looking
    at ANOTHER role's mail (coordinator-claude's relay client does), so that role
    isn't recorded as seen.

    `after` (2026-09-26, mail-watch pilot): only messages with
    message_id > after. A session's background mail watch polls with
    after=<last id it handled>, so mail it deliberately leaves unread
    (e.g. waiting on the operator) doesn't keep re-waking it.
    A session calls this to get its pending messages in order, then works
    through them one by one, calling POST /messages/{id}/read as it
    processes each one. delivered_at is untouched here -- see
    /messages/{id}/delivered-batch, which is coordinator-claude's own separate
    'did my live nudge transport succeed' bookkeeping."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT d.message_id, th.group_id, th.thread_id, th.topic,
                      m.sender_role, m.body, m.created_at, d.delivered_at
               FROM deliveries d
               JOIN messages m ON m.message_id = d.message_id
               JOIN threads th ON th.thread_id = m.thread_id
               JOIN group_members gm ON gm.group_id = th.group_id
                    AND gm.role = d.recipient_role AND gm.left_at IS NULL
               WHERE d.recipient_role = $1 AND d.read_at IS NULL
                     AND d.recipient_role != m.sender_role
                     AND d.message_id > $2
               ORDER BY d.message_id""",
            role, after,
        )
        if not peek:
            await _touch(conn, role, f"checked mail ({len(rows)} unread)" if not after
                         else f"checked mail ({len(rows)} newer than #{after})")
    return {"pending": [dict(r) for r in rows]}


@app.get("/threads/{thread_id}/history")
async def get_thread_history(thread_id: int):
    """acked_by lists roles that gave this message a thumbs-up (info-only,
    no action needed) rather than posting a full reply -- lets anyone
    glance at a thread and see who's silently signed off vs. who hasn't
    responded, without that showing up as reply noise in the message list
    itself."""
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM threads WHERE thread_id = $1", thread_id)
        if not exists:
            raise HTTPException(404, f"thread {thread_id} does not exist")
        rows = await conn.fetch(
            """SELECT message_id, sender_role, body, created_at
               FROM messages WHERE thread_id = $1 ORDER BY message_id""",
            thread_id,
        )
        acks = await conn.fetch(
            """SELECT d.message_id, d.recipient_role
               FROM deliveries d JOIN messages m ON m.message_id = d.message_id
               WHERE m.thread_id = $1 AND d.ack_at IS NOT NULL
               ORDER BY d.message_id, d.recipient_role""",
            thread_id,
        )
    acked_by: dict[int, list] = {}
    for a in acks:
        acked_by.setdefault(a["message_id"], []).append(a["recipient_role"])
    return {"messages": [{**r, "acked_by": acked_by.get(r["message_id"], [])} for r in rows]}


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


async def _fetch_recent_threads(conn: agora_db.Conn):
    """Last 10 threads by most recent activity, plus their messages.
    Shared by the full dashboard render and the polling fragment endpoint
    so the two never drift out of sync."""
    recent_thread_rows = await conn.fetch(
        """SELECT t.thread_id, t.group_id, t.topic, t.status,
                  COUNT(m.message_id) AS message_count,
                  MAX(m.created_at) AS last_message_at
           FROM threads t
           LEFT JOIN messages m ON m.thread_id = t.thread_id
           GROUP BY t.thread_id, t.group_id, t.topic, t.status, t.created_at
           ORDER BY COALESCE(MAX(m.created_at), t.created_at) DESC
           LIMIT 10"""
    )
    recent_thread_ids = [t["thread_id"] for t in recent_thread_rows]
    recent_messages_rows = await conn.fetch(
        f"""SELECT thread_id, message_id, sender_role, body, created_at
           FROM messages WHERE thread_id IN ({ph(1, len(recent_thread_ids))})
           ORDER BY thread_id, message_id""",
        *recent_thread_ids,
    ) if recent_thread_ids else []

    recent_message_ids = [m["message_id"] for m in recent_messages_rows]
    delivery_rows = await conn.fetch(
        f"""SELECT d.message_id, d.recipient_role, d.delivered_at, d.read_at, d.ack_at
           FROM deliveries d
           JOIN messages m ON m.message_id = d.message_id
           WHERE d.message_id IN ({ph(1, len(recent_message_ids))}) AND d.recipient_role != m.sender_role
           ORDER BY d.message_id, d.recipient_role""",
        *recent_message_ids,
    ) if recent_message_ids else []

    recent_messages_by_thread: dict[int, list] = {}
    for m in recent_messages_rows:
        recent_messages_by_thread.setdefault(m["thread_id"], []).append(m)

    deliveries_by_message: dict[int, list] = {}
    for d in delivery_rows:
        deliveries_by_message.setdefault(d["message_id"], []).append(d)

    return recent_thread_rows, recent_messages_by_thread, deliveries_by_message


def _render_delivery_status_html(deliveries: list) -> str:
    """One message's per-recipient status, bucketed by furthest stage
    reached: acked (thumbs-up, no reply needed) > read (processed, may
    have replied) > delivered (nudged, not yet pulled) > pending (not
    even nudged yet, or nudge failed)."""
    acked = sorted(d["recipient_role"] for d in deliveries if d["ack_at"])
    read_only = sorted(d["recipient_role"] for d in deliveries if d["read_at"] and not d["ack_at"])
    delivered_only = sorted(
        d["recipient_role"] for d in deliveries
        if d["delivered_at"] and not d["read_at"] and not d["ack_at"]
    )
    pending = sorted(
        d["recipient_role"] for d in deliveries
        if not d["delivered_at"] and not d["read_at"] and not d["ack_at"]
    )

    parts = []
    if acked:
        parts.append(f'<span class="status-chip acked">✓ acked: {_esc(", ".join(acked))}</span>')
    if read_only:
        parts.append(f'<span class="status-chip read">read: {_esc(", ".join(read_only))}</span>')
    if delivered_only:
        parts.append(f'<span class="status-chip delivered">nudged: {_esc(", ".join(delivered_only))}</span>')
    if pending:
        parts.append(f'<span class="status-chip pending">pending: {_esc(", ".join(pending))}</span>')
    return "".join(parts) or '<span class="meta">no recipients</span>'


def _render_recent_threads_html(recent_thread_rows, recent_messages_by_thread, deliveries_by_message) -> str:
    recent_threads_html = ""
    for t in recent_thread_rows:
        msgs = recent_messages_by_thread.get(t["thread_id"], [])
        msg_rows_html = "".join(
            f"""<tr>
                    <td class="meta">{m['message_id']}</td>
                    <td>{_esc(m['sender_role'])}</td>
                    <td>{_esc(m['body'])}</td>
                    <td class="meta">{_esc(m['created_at'])}</td>
                    <td>{_render_delivery_status_html(deliveries_by_message.get(m['message_id'], []))}</td>
                </tr>"""
            for m in msgs
        ) or '<tr><td colspan="5"><em>no messages</em></td></tr>'
        recent_threads_html += f"""
        <details class="thread-row" data-thread-id="{t['thread_id']}">
            <summary>
                <span class="thread-id">#{t['thread_id']}</span>
                <span class="thread-topic">{_esc(t['topic'])}</span>
                <span class="badge {_esc(t['status'])}">{_esc(t['status'])}</span>
                <span class="meta">{t['message_count']} msg{'s' if t['message_count'] != 1 else ''} · group {t['group_id']} · last activity {_esc(t['last_message_at']) or '—'}</span>
            </summary>
            <table>
                <thead><tr><th>ID</th><th>Sender</th><th>Body</th><th>Posted</th><th>Recipients</th></tr></thead>
                <tbody>{msg_rows_html}</tbody>
            </table>
        </details>"""
    return recent_threads_html or "<p><em>no threads yet</em></p>"


@app.get("/dashboard/recent-threads", response_class=HTMLResponse)
async def dashboard_recent_threads_fragment():
    """Just the 'Recent threads' fragment, for the dashboard's own polling
    JS to re-fetch every few seconds without reloading the whole page."""
    async with pool.acquire() as conn:
        recent_thread_rows, recent_messages_by_thread, deliveries_by_message = await _fetch_recent_threads(conn)
    return HTMLResponse(content=_render_recent_threads_html(recent_thread_rows, recent_messages_by_thread, deliveries_by_message))


_NO_CACHE = {"Cache-Control": "no-cache"}
# Backstop for the dashboard's Markdown renderer (security-claude, msg 758):
# scripts only from this origin, no framing, no plugins, no base/form tricks.
# Inline style attributes stay allowed (the page sets element styles).
_DASHBOARD_HEADERS = {
    **_NO_CACHE,
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_DASHBOARD_STATIC = {"dashboard.css", "dashboard.js"}


@app.get("/dashboard")
@app.get("/dashboard/threads/{thread_id}")
async def dashboard(thread_id: Optional[int] = None):
    """Read-only coordination console for the operator (2026-09-25 redesign). The
    same static page serves every thread URL; dashboard.js reads
    location.pathname and polls /api/dashboard/state for data."""
    return FileResponse(STATIC_DIR / "dashboard.html", headers=_DASHBOARD_HEADERS)


@app.get("/dashboard/static/{name}")
async def dashboard_static(name: str):
    if name not in _DASHBOARD_STATIC:
        raise HTTPException(404, "not found")
    return FileResponse(STATIC_DIR / name, headers=_DASHBOARD_HEADERS)


@app.get("/api/dashboard/state")
async def dashboard_state(since_id: int = 0, since_ts: Optional[datetime] = None):
    """Everything the dashboard needs, incrementally. Roles, groups and
    threads are small and always sent whole. Messages are only those with
    message_id > since_id, EXCEPT on a first load (since_id=0), which is
    capped to the most recent 1000 -- otherwise this grows without bound as
    the DB accumulates history, both in response size and in what an open
    dashboard tab holds in memory forever. A first load also always
    includes every flagged (needs_operator) message even if it's older
    than that cap, so an old open ask can never silently disappear from
    "Needs you"; message_counts (thread_id -> total, always sent, like
    threads) lets the client show the true count for a thread even when
    older messages weren't loaded. Deliveries are those of the loaded
    messages on a first load, or of new messages plus any whose
    delivered/read/ack time moved after since_ts on an incremental poll
    (overlapping by a few seconds so a write racing the previous poll
    isn't missed; the client merges idempotently). Pass back server_time
    as the next since_ts. Delivery stage is the furthest reached:
    a(cked) > r(ead) > n(udged) > p(ending); the sender's own row is
    excluded."""
    since_cutoff = since_ts - timedelta(seconds=10) if since_ts else None
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        # One snapshot for every query below, so the parts agree.
        server_time = await conn.fetchval("SELECT UTC_TIMESTAMP(6)")
        roles = await conn.fetch(
            """SELECT b.role, b.session_name, b.status, b.bound_at, r.last_seen_at, r.last_seen_action,
                      r.last_action_at, r.heartbeat_at, r.heartbeat_interval
               FROM role_bindings b JOIN roles r ON r.role = b.role ORDER BY b.role"""
        )
        group_ids = await conn.fetch("SELECT group_id FROM `groups`")
        member_rows = await conn.fetch(
            "SELECT group_id, role FROM group_members WHERE left_at IS NULL ORDER BY group_id, role"
        )
        members_by_group: dict[int, list] = {g["group_id"]: [] for g in group_ids}
        for r in member_rows:
            members_by_group.setdefault(r["group_id"], []).append(r["role"])
        threads = await conn.fetch(
            "SELECT thread_id, group_id, topic, status, created_at FROM threads ORDER BY thread_id"
        )
        message_counts = await conn.fetch(
            "SELECT thread_id, count(*) AS n FROM messages GROUP BY thread_id"
        )
        if since_id == 0:
            # Union of the most recent 1000 and every flagged message, so
            # an old open (or recently answered/cleared) ask always has
            # its message loaded alongside it.
            messages = await conn.fetch(
                """WITH recent AS (SELECT message_id FROM messages ORDER BY message_id DESC LIMIT 1000),
                        flagged AS (SELECT message_id FROM messages WHERE attn_kind IS NOT NULL),
                        loaded AS (SELECT message_id FROM recent UNION SELECT message_id FROM flagged)
                   SELECT m.message_id, m.thread_id, m.sender_role, m.body, m.created_at
                   FROM messages m JOIN loaded l ON l.message_id = m.message_id
                   ORDER BY m.message_id"""
            )
        else:
            messages = await conn.fetch(
                """SELECT message_id, thread_id, sender_role, body, created_at
                   FROM messages WHERE message_id > $1 ORDER BY message_id""",
                since_id,
            )
        # Flagged messages: all of them on a full load; afterwards only the
        # open ones plus any answered/cleared since the last poll.
        attention = await conn.fetch(
            """SELECT m.message_id, m.attn_kind, m.attn_why, m.attn_answered_by, m.attn_cleared_at
               FROM messages m LEFT JOIN messages a ON a.message_id = m.attn_answered_by
               WHERE m.attn_kind IS NOT NULL
                 AND ($1 IS NULL
                      OR m.message_id > $2
                      OR (m.attn_answered_by IS NULL AND m.attn_cleared_at IS NULL)
                      OR m.attn_cleared_at > $1
                      OR a.created_at > $1)
               ORDER BY m.message_id""",
            since_cutoff, since_id,
        )
        if since_id == 0:
            # Same loaded-message set as `messages` above, not "every
            # delivery ever" -- otherwise this stays uncapped regardless
            # of the messages cap.
            loaded_ids = [m["message_id"] for m in messages]
            deliveries = await conn.fetch(
                f"""SELECT d.message_id, d.recipient_role,
                          CASE WHEN d.ack_at IS NOT NULL THEN 'a'
                               WHEN d.read_at IS NOT NULL THEN 'r'
                               WHEN d.delivered_at IS NOT NULL THEN 'n'
                               ELSE 'p' END AS stage
                   FROM deliveries d JOIN messages m ON m.message_id = d.message_id
                   WHERE d.recipient_role <> m.sender_role AND d.message_id IN ({ph(1, len(loaded_ids))})
                   ORDER BY d.message_id""",
                *loaded_ids,
            ) if loaded_ids else []
        else:
            deliveries = await conn.fetch(
                """SELECT d.message_id, d.recipient_role,
                          CASE WHEN d.ack_at IS NOT NULL THEN 'a'
                               WHEN d.read_at IS NOT NULL THEN 'r'
                               WHEN d.delivered_at IS NOT NULL THEN 'n'
                               ELSE 'p' END AS stage
                   FROM deliveries d JOIN messages m ON m.message_id = d.message_id
                   WHERE d.recipient_role <> m.sender_role
                     AND (d.message_id > $1
                          OR d.delivered_at > $2 OR d.read_at > $2 OR d.ack_at > $2)
                   ORDER BY d.message_id""",
                since_id,
                since_cutoff,
            )
    return {
        "server_time": server_time,
        "roles": [dict(r) for r in roles],
        "groups": members_by_group,
        "threads": [dict(t) for t in threads],
        "message_counts": {c["thread_id"]: c["n"] for c in message_counts},
        "messages": [dict(m) for m in messages],
        "deliveries": [[d["message_id"], d["recipient_role"], d["stage"]] for d in deliveries],
        # Answers/clears change old messages, which the since_id cursor
        # alone would miss; the client merges these by message_id.
        "attention": [dict(a) for a in attention],
    }


@app.get("/dashboard/classic", response_class=HTMLResponse)
async def dashboard_classic():
    """The pre-2026-09-25 server-rendered view, kept as a fallback while
    the redesigned /dashboard beds in. Read-only human view: authoritative
    session per role, groups and their members, threads and their topics.
    No auth -- same trusted-peer threat model as the rest of this system."""
    async with pool.acquire() as conn:
        bindings = await conn.fetch(
            "SELECT role, session_name, status, bound_at FROM role_bindings ORDER BY role"
        )
        group_rows = await conn.fetch("SELECT group_id, created_at FROM `groups` ORDER BY group_id")
        classic_members = await conn.fetch(
            "SELECT group_id, role FROM group_members WHERE left_at IS NULL ORDER BY group_id, role"
        )
        members_of: dict[int, list] = {}
        for r in classic_members:
            members_of.setdefault(r["group_id"], []).append(r["role"])
        thread_rows = await conn.fetch(
            """SELECT t.thread_id, t.group_id, t.topic, t.status, t.created_at,
                      COUNT(m.message_id) AS message_count,
                      MAX(m.created_at) AS last_message_at
               FROM threads t
               LEFT JOIN messages m ON m.thread_id = t.thread_id
               GROUP BY t.thread_id, t.group_id, t.topic, t.status, t.created_at
               ORDER BY t.group_id, t.thread_id"""
        )
        recent_thread_rows, recent_messages_by_thread, deliveries_by_message = await _fetch_recent_threads(conn)

    threads_by_group: dict[int, list] = {}
    for t in thread_rows:
        threads_by_group.setdefault(t["group_id"], []).append(t)

    binding_rows_html = "".join(
        f"""<tr class="{_esc(b['status'])}">
                <td>{_esc(b['role'])}</td>
                <td>{_esc(b['session_name'])}</td>
                <td><span class="badge {_esc(b['status'])}">{_esc(b['status'])}</span></td>
                <td>{_esc(b['bound_at'])}</td>
            </tr>"""
        for b in bindings
    )

    group_sections_html = ""
    for g in group_rows:
        members_html = ", ".join(_esc(m) for m in members_of.get(g["group_id"], [])) or "<em>no members</em>"
        thread_rows_html = "".join(
            f"""<tr class="{_esc(t['status'])}">
                    <td>{t['thread_id']}</td>
                    <td>{_esc(t['topic'])}</td>
                    <td><span class="badge {_esc(t['status'])}">{_esc(t['status'])}</span></td>
                    <td>{t['message_count']}</td>
                    <td>{_esc(t['last_message_at']) or '—'}</td>
                </tr>"""
            for t in threads_by_group.get(g["group_id"], [])
        ) or '<tr><td colspan="5"><em>no threads yet</em></td></tr>'
        group_sections_html += f"""
        <div class="group-card">
            <h3>Group {g['group_id']} <span class="members">— {members_html}</span></h3>
            <table>
                <thead><tr><th>Thread</th><th>Topic</th><th>Status</th><th># msgs</th><th>Last activity</th></tr></thead>
                <tbody>{thread_rows_html}</tbody>
            </table>
        </div>"""

    recent_threads_html = _render_recent_threads_html(recent_thread_rows, recent_messages_by_thread, deliveries_by_message)

    page = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Claude Agora — coordination dashboard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; margin: 2rem; background: #0f1115; color: #e6e6e6; }}
  h1 {{ margin-bottom: 0.2rem; }}
  h2 {{ margin-top: 2.5rem; border-bottom: 1px solid #333; padding-bottom: 0.3rem; }}
  h3 {{ margin-bottom: 0.4rem; }}
  .subtitle {{ color: #888; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.7rem; border-bottom: 1px solid #262a33; }}
  th {{ color: #9aa4b2; font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }}
  tr.stale {{ opacity: 0.55; }}
  .badge {{ padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.8rem; }}
  .badge.live, .badge.active {{ background: #17351f; color: #6fd67f; }}
  .badge.stale, .badge.archived {{ background: #332a17; color: #d6ac6f; }}
  .group-card {{ background: #171a21; border: 1px solid #262a33; border-radius: 8px; padding: 1rem 1.2rem; margin-bottom: 1rem; }}
  .members {{ color: #9aa4b2; font-weight: normal; font-size: 0.95rem; }}
  .meta {{ color: #666; font-size: 0.85rem; }}
  details.thread-row {{ background: #171a21; border: 1px solid #262a33; border-radius: 8px; padding: 0.6rem 1rem; margin-bottom: 0.5rem; }}
  details.thread-row[open] {{ padding-bottom: 1rem; }}
  details.thread-row summary {{ cursor: pointer; list-style: none; display: flex; align-items: center; gap: 0.7rem; flex-wrap: wrap; }}
  details.thread-row summary::-webkit-details-marker {{ display: none; }}
  details.thread-row summary::before {{ content: "▸"; color: #666; transition: transform 0.1s; }}
  details.thread-row[open] summary::before {{ transform: rotate(90deg); }}
  .thread-id {{ color: #666; font-family: monospace; }}
  .thread-topic {{ font-weight: 600; }}
  details.thread-row table {{ margin-top: 0.8rem; margin-bottom: 0; }}
  .status-chip {{ display: inline-block; padding: 0.05rem 0.45rem; border-radius: 999px; font-size: 0.75rem; margin: 0.1rem 0.25rem 0.1rem 0; white-space: nowrap; }}
  .status-chip.acked {{ background: #17351f; color: #6fd67f; }}
  .status-chip.read {{ background: #17293a; color: #6fa8d6; }}
  .status-chip.delivered {{ background: #332a17; color: #d6ac6f; }}
  .status-chip.pending {{ background: #2a2a2a; color: #888; }}
  .live-dot {{ display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 50%; background: #6fd67f; margin-right: 0.4rem; animation: pulse 1.6s ease-in-out infinite; }}
  .live-dot.stalled {{ background: #d66f6f; animation: none; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
  #recent-threads-status {{ color: #666; font-size: 0.8rem; margin-bottom: 0.6rem; }}
</style>
</head>
<body>
<h1>Claude Agora</h1>
<p class="subtitle">Coordination hub — sessions, groups, threads. Groups/sessions below reload with the page; threads update live.</p>

<h2>Recent threads</h2>
<p id="recent-threads-status"><span class="live-dot" id="live-dot"></span><span id="live-text">live</span></p>
<div id="recent-threads">{recent_threads_html}</div>
<script>
(function() {{
  var POLL_MS = 4000;
  var container = document.getElementById('recent-threads');
  var dot = document.getElementById('live-dot');
  var text = document.getElementById('live-text');

  function refresh() {{
    fetch('/dashboard/recent-threads')
      .then(function(r) {{ if (!r.ok) throw new Error(r.status); return r.text(); }})
      .then(function(html) {{
        var openIds = Array.prototype.slice.call(
          container.querySelectorAll('details[open]')
        ).map(function(d) {{ return d.getAttribute('data-thread-id'); }});

        container.innerHTML = html;

        openIds.forEach(function(id) {{
          var el = container.querySelector('details[data-thread-id="' + id + '"]');
          if (el) el.setAttribute('open', '');
        }});

        dot.classList.remove('stalled');
        text.textContent = 'live — updated ' + new Date().toLocaleTimeString();
      }})
      .catch(function() {{
        dot.classList.add('stalled');
        text.textContent = 'connection lost, retrying…';
      }});
  }}

  setInterval(refresh, POLL_MS);
}})();
</script>

<h2>Authoritative sessions</h2>
<table>
  <thead><tr><th>Role</th><th>Session name</th><th>Status</th><th>Bound at</th></tr></thead>
  <tbody>{binding_rows_html}</tbody>
</table>

<h2>Groups &amp; threads</h2>
{group_sections_html}

<p class="meta">Claude Agora coordination hub — see README.md / BUILD.md in the repo for the full protocol.</p>
</body>
</html>"""
    return HTMLResponse(content=page)


@app.get("/health")
async def health():
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}

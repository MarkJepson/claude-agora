"""Weekly stale-thread sweep, run by the relay itself.

The policy lives in `plan()`, a pure function (no database, no clock of its
own) so it can be unit tested. `run_sweep()` fetches the active threads, plans,
and applies the plan; `loop()` is the scheduler the API starts at boot.

Policy, per active thread:
  * No `Stale-thread check` yet and no activity for STALE_DAYS: post one.
  * A check exists with no reply after it and a recipient acked it: archive (resolved).
  * A check exists with no reply after it and it is STALE_DAYS old: archive,
    unless the thread is a security finding, which stays open and is escalated
    to the operator once the check is twice that old.
  * A reply saying "still open" keeps the thread and suppresses new checks for
    STILL_OPEN_DAYS. Any other reply counts as activity (the thread is not
    archived on a guess about what the reply meant) and restarts the clock.

Only an ack archives a thread on a human's word; nothing is inferred from
free text except the explicit phrase "still open".

Posts are made as SWEEP_SENDER_ROLE and go to the group's current members, so
the sender does not need to be a member of the group.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import agora_db
from agora_db import ph

log = logging.getLogger("uvicorn.error")

JOB = "stale-sweep"
CHECK_PREFIX = "Stale-thread check"
ESCALATION_PREFIX = "Stale-thread escalation"
STALE_DAYS = 7
STILL_OPEN_DAYS = 28
SECURITY_WORDS = ("security finding", "vulnerab", "exposure")


def _is_security(thread: dict) -> bool:
    topic = (thread.get("topic") or "").lower()
    if any(w in topic for w in SECURITY_WORDS):
        return True
    return any(
        m.get("attn_kind") == "security" and not m.get("attn_answered_by") and not m.get("attn_cleared_at")
        for m in thread["messages"]
    )


def _plan_thread(thread: dict, now: datetime, sender: str) -> dict | None:
    msgs = sorted(thread["messages"], key=lambda m: m["message_id"])
    stale = timedelta(days=STALE_DAYS)

    def ours(m, prefix):
        return m["sender_role"] == sender and m["head"].startswith(prefix)

    checks = [m for m in msgs if ours(m, CHECK_PREFIX)]
    # Our own sweep posts are not activity.
    activity = [m for m in msgs if not (ours(m, CHECK_PREFIX) or ours(m, ESCALATION_PREFIX))]
    last = max([m["created_at"] for m in activity] or [thread["created_at"]])

    if checks:
        check = checks[-1]
        replies = [m for m in msgs if m["message_id"] > check["message_id"] and m in activity]
        if not replies:
            if check["acked"]:
                return {"action": "archive", "reason": "acked by a recipient"}
            age = now - check["created_at"]
            if age < stale:
                return None
            if not _is_security(thread):
                return {"action": "archive", "reason": "no reply to the stale-thread check"}
            escalated = any(ours(m, ESCALATION_PREFIX) and m["message_id"] > check["message_id"] for m in msgs)
            if age >= 2 * stale and not escalated:
                return {"action": "escalate", "reason": "security finding unconfirmed after two sweeps"}
            return None
        # A reply exists: keep the thread; "still open" suppresses re-prompting.
        still_open = [m for m in replies if "still open" in m["head"].lower()]
        if still_open and now - still_open[-1]["created_at"] < timedelta(days=STILL_OPEN_DAYS):
            return None
    if now - last >= stale:
        return {"action": "prompt", "reason": f"no activity since {last.date().isoformat()}", "since": last.date().isoformat()}
    return None


def plan(threads: list[dict], now: datetime, sender: str) -> list[dict]:
    out = []
    for t in threads:
        step = _plan_thread(t, now, sender)
        if step:
            out.append({"thread_id": t["thread_id"], "topic": t["topic"], **step})
    return out


async def fetch_threads(conn: agora_db.Conn) -> list[dict]:
    threads = {
        r["thread_id"]: {**dict(r), "messages": []}
        for r in await conn.fetch("SELECT thread_id, topic, created_at FROM threads WHERE status = 'active'")
    }
    rows = await conn.fetch(
        """SELECT m.message_id, m.thread_id, m.sender_role, LEFT(m.body, 200) AS head, m.created_at,
                  m.attn_kind, m.attn_answered_by, m.attn_cleared_at,
                  EXISTS(SELECT 1 FROM deliveries d WHERE d.message_id = m.message_id
                         AND d.ack_at IS NOT NULL AND d.recipient_role <> m.sender_role) AS acked
           FROM messages m JOIN threads t ON t.thread_id = m.thread_id
           WHERE t.status = 'active' ORDER BY m.message_id"""
    )
    for r in rows:
        threads[r["thread_id"]]["messages"].append({**dict(r), "acked": bool(r["acked"])})
    return list(threads.values())


def _body(step: dict) -> tuple[str, tuple[str, str] | None]:
    if step["action"] == "prompt":
        return (
            f"{CHECK_PREFIX}: no activity since {step['since']}. Ack this message or reply to confirm the "
            "topic is resolved; reply \"still open\" to keep it. No reply by the next weekly sweep means this "
            "thread is archived (security findings are kept open until the owner confirms).",
            None,
        )
    return (
        f"{ESCALATION_PREFIX}: this security finding is still unconfirmed after two weekly sweeps. "
        "It stays open until its owner confirms it is resolved.",
        ("security", "Unconfirmed security finding after two sweeps"),
    )


async def _post(conn: agora_db.Conn, thread_id: int, sender: str, body: str, attn) -> bool:
    row = await conn.fetchrow("SELECT group_id, status FROM threads WHERE thread_id = $1", thread_id)
    if row is None or row["status"] != "active":
        return False
    members = [
        r["role"] for r in await conn.fetch(
            "SELECT role FROM group_members WHERE group_id = $1 AND left_at IS NULL", row["group_id"],
        )
    ]
    if not members:
        return False
    message_id = await conn.insert(
        "INSERT INTO messages (thread_id, sender_role, body, attn_kind, attn_why) VALUES ($1, $2, $3, $4, $5)",
        thread_id, sender, body, attn[0] if attn else None, attn[1] if attn else None,
    )
    for role in members:
        await conn.execute(
            "INSERT INTO deliveries (message_id, recipient_role, delivered_at) VALUES ($1, $2, NULL)",
            message_id, role,
        )
    return True


async def apply(pool, steps: list[dict], sender: str) -> dict:
    done = {"prompted": [], "archived": [], "escalated": [], "skipped": []}
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM roles WHERE role = $1", sender)
        if not exists:
            raise RuntimeError(f"sweep sender role {sender!r} is not registered")
        for s in steps:
            tid = s["thread_id"]
            async with conn.transaction():
                if s["action"] == "archive":
                    n = await conn.execute(
                        "UPDATE threads SET status = 'archived' WHERE thread_id = $1 AND status = 'active'", tid,
                    )
                    ok = n > 0
                else:
                    body, attn = _body(s)
                    ok = await _post(conn, tid, sender, body, attn)
            key = {"prompt": "prompted", "archive": "archived", "escalate": "escalated"}[s["action"]]
            done[key if ok else "skipped"].append(tid)
    return done


async def preview(pool, now: datetime | None = None, sender: str | None = None) -> list[dict]:
    sender = sender or sweep_sender()
    async with pool.acquire() as conn:
        threads = await fetch_threads(conn)
    return plan(threads, now or datetime.now(timezone.utc), sender)


async def run_sweep(pool, now: datetime | None = None, sender: str | None = None) -> dict:
    sender = sender or sweep_sender()
    steps = await preview(pool, now, sender)
    result = await apply(pool, steps, sender)
    result["planned"] = len(steps)
    return result


def sweep_sender() -> str:
    return os.environ.get("SWEEP_SENDER_ROLE", "scrum-claude")


def enabled() -> bool:
    return os.environ.get("SWEEP_ENABLED", "").strip().lower() in ("1", "true", "yes")


def interval() -> timedelta:
    return timedelta(hours=float(os.environ.get("SWEEP_INTERVAL_HOURS", "168")))


async def claim(pool, now: datetime) -> datetime | None | bool:
    """Atomically take this interval's run. Returns the previous run time (None
    if it never ran) when claimed, or False when another run holds it. The
    UPDATE's WHERE is the lock, so two replicas cannot both claim."""
    async with pool.acquire() as conn:
        await conn.execute("INSERT IGNORE INTO job_runs (job) VALUES ($1)", JOB)
        prev = await conn.fetchval("SELECT last_run_at FROM job_runs WHERE job = $1", JOB)
        n = await conn.execute(
            """UPDATE job_runs SET last_run_at = $1
               WHERE job = $2 AND (last_run_at IS NULL OR last_run_at <= $3)
                     AND last_run_at <=> $4""",
            now, JOB, now - interval(), prev,
        )
    return prev if n else False


async def record(pool, result: dict) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE job_runs SET last_result = $1 WHERE job = $2", json.dumps(result), JOB)


async def release(pool, prev, now: datetime) -> None:
    """A failed run gives its claim back so the next tick retries."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE job_runs SET last_run_at = $1 WHERE job = $2 AND last_run_at = $3", prev, JOB, now,
        )


async def last_run(pool) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_run_at, last_result FROM job_runs WHERE job = $1", JOB)
    if row is None:
        return {"last_run_at": None, "last_result": None}
    return {"last_run_at": row["last_run_at"], "last_result": json.loads(row["last_result"]) if row["last_result"] else None}


async def tick(pool) -> dict | None:
    now = datetime.now(timezone.utc)
    prev = await claim(pool, now)
    if prev is False:
        return None
    try:
        result = await run_sweep(pool, now)
    except Exception:
        log.exception("stale-thread sweep failed; releasing its claim")
        await release(pool, prev, now)
        return None
    await record(pool, result)
    log.info("stale-thread sweep: %s", result)
    return result


async def loop(pool) -> None:
    check_every = float(os.environ.get("SWEEP_CHECK_SECONDS", "900"))
    while True:
        try:
            await tick(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sweep scheduler tick failed")
        await asyncio.sleep(check_every)

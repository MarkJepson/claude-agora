"""MariaDB access for the relay: a thin, asyncpg-shaped layer over aiomysql, so
main.py keeps its `pool.acquire()` / `conn.fetch*` / `conn.transaction()` shape
and only the SQL dialect differs.

What it does for callers:
  * `$1, $2` placeholders (repeats and any order are fine) become `%s`.
  * Rows come back as dicts. DATETIME(6) columns hold UTC, so naive datetimes
    coming out are made UTC-aware, and aware datetimes going in are converted
    to naive UTC. main.py keeps using timezone-aware datetimes throughout.
  * `execute()` returns the matched-row count (CLIENT.FOUND_ROWS, so an UPDATE
    that leaves a row unchanged still counts, as in Postgres); `insert()`
    returns the AUTO_INCREMENT id.
  * `retry_on_db_error` re-runs a whole request handler after a deadlock.

DATABASE_URL: mysql://user:password@host:3306/dbname[?ssl=true][&ssl_ca=/path/ca.pem]
(`mariadb://` is accepted too). Percent-encode any of @ / : % in the password,
or leave it out of the URL and put it in DATABASE_PASSWORD, which wins.
Message ids come from AUTO_INCREMENT, and the mail watch's "highest message_id
seen" cursor needs them to keep growing, so point every API instance at a
single writer.
"""
import asyncio
import functools
import logging
import os
import re
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import aiomysql
import pymysql
from pymysql.constants import CLIENT

log = logging.getLogger("uvicorn.error")

_PARAM = re.compile(r"\$(\d+)")

# Errors after which the transaction is known to have rolled back, so re-running
# the handler cannot double-apply it: 1213 deadlock, 1205 lock wait timeout,
# 1047 server not ready. Lost
# connections (2006/2013) are deliberately NOT retried: if the COMMIT had
# already succeeded, a retry would post the message twice. Stale pooled
# connections are handled before the handler runs (see Pool.acquire).
_RETRYABLE_CODES = {1213, 1205, 1047}
# Startup DDL can also collide with another replica's DDL (1061 duplicate key name).
_DDL_RETRYABLE = _RETRYABLE_CODES | {1061}


def ph(start: int, n: int) -> str:
    """`$start,$start+1,...` for an IN (...) list of n values."""
    return ",".join(f"${i}" for i in range(start, start + n))


def _to_db(v):
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _translate(sql: str, args: tuple):
    if not args:
        return sql, None
    order: list[int] = []

    def sub(m):
        order.append(int(m.group(1)) - 1)
        return "%s"

    out = _PARAM.sub(sub, sql.replace("%", "%%"))
    return out, tuple(_to_db(args[i]) for i in order)


def _row(r: dict) -> dict:
    return {
        k: (v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v)
        for k, v in r.items()
    }


class Conn:
    def __init__(self, raw: aiomysql.Connection):
        self._c = raw

    async def _exec(self, sql: str, args: tuple, want: str):
        q, a = _translate(sql, args)
        async with self._c.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(q, a)
            if want == "all":
                return [_row(r) for r in await cur.fetchall()]
            if want == "one":
                r = await cur.fetchone()
                return _row(r) if r is not None else None
            if want == "rowcount":
                return cur.rowcount
            return cur.lastrowid

    async def fetch(self, sql: str, *args) -> list[dict]:
        return await self._exec(sql, args, "all")

    async def fetchrow(self, sql: str, *args) -> Optional[dict]:
        return await self._exec(sql, args, "one")

    async def fetchval(self, sql: str, *args):
        r = await self._exec(sql, args, "one")
        return next(iter(r.values())) if r else None

    async def execute(self, sql: str, *args) -> int:
        """Matched-row count."""
        return await self._exec(sql, args, "rowcount")

    async def insert(self, sql: str, *args) -> int:
        """Runs an INSERT and returns the new AUTO_INCREMENT id."""
        return await self._exec(sql, args, "insert")

    @asynccontextmanager
    async def transaction(self, isolation: Optional[str] = None, readonly: bool = False):
        """BEGIN ... COMMIT (ROLLBACK on any exception). `isolation` applies to
        this transaction only, e.g. "repeatable_read" for a consistent
        multi-query snapshot. The pool's default is InnoDB's REPEATABLE READ."""
        if isolation:
            await self.execute("SET TRANSACTION ISOLATION LEVEL " + isolation.replace("_", " ").upper())
        await self.execute("START TRANSACTION READ ONLY" if readonly else "START TRANSACTION")
        try:
            yield self
        except BaseException:
            try:
                await self._c.rollback()
            except Exception:
                log.warning("rollback failed (connection lost?); re-raising the original error")
            raise
        else:
            await self._c.commit()


class Pool:
    def __init__(self, pool: aiomysql.Pool):
        self._p = pool

    @asynccontextmanager
    async def acquire(self):
        async with self._p.acquire() as raw:
            # A pooled connection can have been dropped by a server restart or
            # a proxy idle timeout; reconnect instead of failing.
            await raw.ping(reconnect=True)
            yield Conn(raw)

    async def close(self) -> None:
        self._p.close()
        await self._p.wait_closed()


async def create_pool(url: str, min_size: int = 1, max_size: int = 5, wait_seconds: int = 60) -> Pool:
    u = urlparse(url)
    if u.scheme not in ("mysql", "mariadb"):
        raise ValueError(f"DATABASE_URL must be mysql://... (got scheme {u.scheme!r})")
    q = parse_qs(u.query)
    ctx = None
    if q.get("ssl_ca"):
        ctx = ssl.create_default_context(cafile=q["ssl_ca"][0])
    elif q.get("ssl", ["false"])[0].lower() in ("1", "true", "yes"):
        ctx = ssl.create_default_context()
    kwargs = dict(
        host=u.hostname or "localhost",
        port=u.port or 3306,
        user=unquote(u.username or ""),
        password=os.environ.get("DATABASE_PASSWORD") or unquote(u.password or ""),
        db=(u.path or "/").lstrip("/"),
        minsize=min_size,
        maxsize=max_size,
        autocommit=True,
        charset="utf8mb4",
        client_flag=CLIENT.FOUND_ROWS,
        init_command="SET time_zone = '+00:00'",
        pool_recycle=280,
        ssl=ctx,
    )
    # The DB may still be starting (compose, or a pod scheduled before it).
    for attempt in range(1, max(1, wait_seconds // 2) + 1):
        try:
            return Pool(await aiomysql.create_pool(**kwargs))
        except (pymysql.err.OperationalError, OSError) as e:
            if attempt * 2 >= wait_seconds:
                raise
            log.warning("database not ready (%s); retrying in 2s", e)
            await asyncio.sleep(2)
    raise RuntimeError("unreachable")


async def apply_schema(conn: Conn, path: Path) -> None:
    """Runs schema.sql; every statement is idempotent (IF NOT EXISTS / INSERT
    IGNORE), so this is safe on every startup."""
    text = "\n".join(l for l in path.read_text().splitlines() if not l.strip().startswith("--"))
    for stmt in (s.strip() for s in text.split(";")):
        if not stmt:
            continue
        for attempt in range(1, 6):
            try:
                await conn.execute(stmt)
                break
            except pymysql.err.MySQLError as e:
                code = e.args[0] if e.args and isinstance(e.args[0], int) else None
                if code not in _DDL_RETRYABLE or attempt == 5:
                    raise
                log.warning("schema statement hit DB error %s (attempt %d); retrying", code, attempt)
                await asyncio.sleep(0.5 * attempt)


def retry_on_db_error(fn, attempts: int = 3):
    """Decorator for request handlers: re-run the whole handler (its
    transaction has been rolled back) on a retryable DB error, such as a
    deadlock."""

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        for attempt in range(1, attempts + 1):
            try:
                return await fn(*a, **kw)
            except pymysql.err.MySQLError as e:
                code = e.args[0] if e.args and isinstance(e.args[0], int) else None
                if code not in _RETRYABLE_CODES or attempt == attempts:
                    raise
                log.warning("retrying %s after DB error %s (attempt %d)", fn.__name__, code, attempt)
                await asyncio.sleep(0.05 * attempt)

    return wrapper

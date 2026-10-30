"""POST /roles/{role}/wait (api/main.py wait_for_mail): the long-poll a mail
watch uses. No database needed: a fake pool stands in for MariaDB. Run from
the repo root with the API's dependencies:

    python3 tests/unit/test_wait.py
"""
import asyncio
import os
import sys
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

_here = Path(__file__).resolve()
for _d in [p / "api" for p in _here.parents] + [Path("/app")]:
    if (_d / "main.py").exists():
        sys.path.insert(0, str(_d))
        break
os.environ.setdefault("DATABASE_URL", "mysql://unused@localhost/unused")
from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402


def row(message_id):
    return {"message_id": message_id, "group_id": 1, "thread_id": 1, "topic": "t",
            "sender_role": "other", "body": "b", "created_at": None, "delivered_at": None}


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchval(self, sql, *args):
        return 1 if self.pool.bound else None

    async def execute(self, sql, *args):
        self.pool.heartbeats.append(args)
        return 1

    async def fetch(self, sql, *args):
        self.pool.sql.append(sql)
        self.pool.pending_queries += 1
        out = list(self.pool.rows)
        if self.pool.on_query:
            self.pool.on_query(self.pool.pending_queries)
        return out


class FakePool:
    def __init__(self, rows=(), bound=True, on_query=None):
        self.rows = list(rows)
        self.bound = bound
        self.on_query = on_query
        self.heartbeats = []
        self.sql = []
        self.pending_queries = 0

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)


class Wait(unittest.TestCase):
    def setUp(self):
        main._waiters.clear()

    def wait(self, pool, known=(), timeout=1):
        main.pool = pool
        return main.wait_for_mail("r", main.WaitRequest(known=list(known)), timeout=timeout)

    def test_returns_at_once_when_there_is_unseen_mail(self):
        pool = FakePool([row(5)])
        t = time.monotonic()
        out = asyncio.run(self.wait(pool, timeout=5))
        self.assertEqual([m["message_id"] for m in out["pending"]], [5])
        self.assertLess(time.monotonic() - t, 0.5)

    def test_known_mail_does_not_end_the_wait(self):
        pool = FakePool([row(5)])
        t = time.monotonic()
        out = asyncio.run(self.wait(pool, known=[5], timeout=1))
        self.assertEqual(out["pending"], [])
        self.assertGreaterEqual(time.monotonic() - t, 0.9)

    def test_a_post_wakes_the_wait(self):
        pool = FakePool()

        async def scenario():
            waiter = asyncio.create_task(self.wait(pool, timeout=10))
            await asyncio.sleep(0.2)
            pool.rows.append(row(7))
            main._notify(["r"])
            return await waiter

        t = time.monotonic()
        out = asyncio.run(scenario())
        self.assertEqual([m["message_id"] for m in out["pending"]], [7])
        self.assertLess(time.monotonic() - t, 1)

    def test_a_post_between_query_and_wait_is_not_missed(self):
        # The post commits (and notifies) right after the first query saw
        # nothing: the wait must still wake at once, not at the timeout.
        def on_query(n):
            if n == 1:
                pool.rows.append(row(9))
                main._notify(["r"])

        pool = FakePool(on_query=on_query)
        t = time.monotonic()
        out = asyncio.run(self.wait(pool, timeout=10))
        self.assertEqual([m["message_id"] for m in out["pending"]], [9])
        self.assertLess(time.monotonic() - t, 1)

    def test_a_wake_for_nothing_new_keeps_waiting(self):
        pool = FakePool([row(5)])

        async def scenario():
            waiter = asyncio.create_task(self.wait(pool, known=[5], timeout=1))
            await asyncio.sleep(0.2)
            main._notify(["r"])
            return await waiter

        t = time.monotonic()
        out = asyncio.run(scenario())
        self.assertEqual(out["pending"], [])
        self.assertGreaterEqual(time.monotonic() - t, 0.9)

    def test_records_the_heartbeat_with_the_timeout_as_interval(self):
        pool = FakePool([row(5)])
        asyncio.run(self.wait(pool, timeout=25))
        self.assertEqual(len(pool.heartbeats), 1)
        self.assertEqual(pool.heartbeats[0][1:], (25, "r"))

    def test_unbound_role_is_404_and_leaves_no_waiter(self):
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(self.wait(FakePool(bound=False)))
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(main._waiters, {})

    def test_waiter_is_removed_after_returning(self):
        asyncio.run(self.wait(FakePool([row(5)])))
        self.assertEqual(main._waiters, {})

    def test_many_waits_for_one_role_are_allowed_and_all_woken(self):
        # A loose per-role limit (WAIT_MAX_PER_ROLE): killed watches' leftover
        # waits must not lock out a re-armed one, and a post wakes them all.
        pool = FakePool()

        async def scenario():
            waits = [asyncio.create_task(self.wait(pool, timeout=10)) for _ in range(10)]
            await asyncio.sleep(0.1)
            pool.rows.append(row(7))
            main._notify(["r"])
            return await asyncio.wait_for(asyncio.gather(*waits), 2)

        outs = asyncio.run(scenario())
        self.assertEqual([[m["message_id"] for m in o["pending"]] for o in outs], [[7]] * 10)
        self.assertEqual(main._waiters, {})

    def test_over_the_per_role_cap_is_503_for_that_role_only(self):
        saved = main.WAIT_MAX_PER_ROLE
        main.WAIT_MAX_PER_ROLE = 2
        pool = FakePool()

        async def scenario():
            waits = [asyncio.create_task(self.wait(pool, timeout=1)) for _ in range(2)]
            other = asyncio.create_task(main.wait_for_mail("s", main.WaitRequest(), timeout=1))
            await asyncio.sleep(0.1)
            try:
                await self.wait(pool, timeout=1)
                third = None
            except HTTPException as e:
                third = e.status_code
            await asyncio.gather(*waits)
            return third, await other   # another role is not affected

        try:
            third, other = asyncio.run(scenario())
            self.assertEqual(third, 503)
            self.assertEqual(other, {"pending": []})
        finally:
            main.WAIT_MAX_PER_ROLE = saved

    def test_over_the_global_safety_cap_is_503(self):
        saved = main.WAIT_MAX_TOTAL
        main.WAIT_MAX_TOTAL = 2
        pool = FakePool()

        async def scenario():
            waits = [asyncio.create_task(self.wait(pool, timeout=1)) for _ in range(2)]
            await asyncio.sleep(0.1)
            try:
                await self.wait(pool, timeout=1)
            finally:
                await asyncio.gather(*waits)

        try:
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(scenario())
            self.assertEqual(cm.exception.status_code, 503)
        finally:
            main.WAIT_MAX_TOTAL = saved

    def test_queries_ids_not_bodies(self):
        pool = FakePool([row(5)])
        asyncio.run(self.wait(pool))
        self.assertTrue(pool.sql)
        for sql in pool.sql:
            self.assertNotIn("m.body", sql)

    def test_more_than_500_known_ids_are_accepted(self):
        main.WaitRequest(known=list(range(2000)))

    def test_zero_timeout_is_422(self):
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(self.wait(FakePool(), timeout=0))
        self.assertEqual(cm.exception.status_code, 422)

    def test_a_long_timeout_is_clamped_not_refused(self):
        pool = FakePool([row(5)])
        asyncio.run(self.wait(pool, timeout=main.WAIT_TIMEOUT_MAX + 30))
        self.assertEqual(pool.heartbeats[0][1], main.WAIT_TIMEOUT_MAX)


if __name__ == "__main__":
    unittest.main()

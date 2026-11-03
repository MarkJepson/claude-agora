"""GET /messages/{id} (api/main.py get_message): one message's full body and
where it lives. No database needed: a fake pool stands in for MariaDB. Run
from the repo root with the API's dependencies:

    python3 tests/unit/test_get_message.py
"""
import asyncio
import os
import sys
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


def message(**attn):
    base = {"message_id": 42, "group_id": 3, "thread_id": 7, "topic": "t",
            "thread_status": "active", "sender_role": "a", "body": "full body\nline 2",
            "created_at": None, "attn_kind": None, "attn_why": None,
            "attn_answered_by": None, "attn_cleared_at": None}
    return {**base, **attn}


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, *args):
        self.pool.sql.append(sql)
        return self.pool.row

    async def fetch(self, sql, *args):
        self.pool.sql.append(sql)
        return [{"message_id": args[0], "recipient_role": r} for r in self.pool.acks]

    async def execute(self, sql, *args):
        self.pool.writes.append(sql)
        return 1


class FakePool:
    def __init__(self, row, acks=()):
        self.row = row
        self.acks = list(acks)
        self.sql = []
        self.writes = []

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)


def get(pool, message_id=42):
    main.pool = pool
    return asyncio.run(main.get_message(message_id))


class GetMessage(unittest.TestCase):
    def test_returns_full_body_thread_and_acks(self):
        out = get(FakePool(message(), acks=["b", "c"]))
        self.assertEqual(out["body"], "full body\nline 2")
        self.assertEqual((out["thread_id"], out["group_id"], out["topic"]), (7, 3, "t"))
        self.assertEqual(out["acked_by"], ["b", "c"])
        self.assertIsNone(out["needs_operator"])
        self.assertFalse(any(k.startswith("attn_") for k in out))

    def test_missing_message_is_404(self):
        with self.assertRaises(HTTPException) as e:
            get(FakePool(None), 99)
        self.assertEqual(e.exception.status_code, 404)

    def test_open_ask(self):
        out = get(FakePool(message(attn_kind="approval", attn_why="why")))
        self.assertEqual(out["needs_operator"], {"kind": "approval", "why": "why",
                                                 "answered_by": None, "cleared_at": None,
                                                 "open": True})

    def test_answered_ask_is_not_open(self):
        out = get(FakePool(message(attn_kind="decision", attn_why="w", attn_answered_by=50)))
        self.assertFalse(out["needs_operator"]["open"])
        self.assertEqual(out["needs_operator"]["answered_by"], 50)

    def test_acks_leave_out_the_sender(self):
        # As the dashboard does (d.recipient_role <> m.sender_role).
        pool = FakePool(message())
        get(pool)
        self.assertIn("d.recipient_role <> m.sender_role", pool.sql[-1])

    def test_reading_writes_nothing(self):
        pool = FakePool(message())
        get(pool)
        self.assertEqual(pool.writes, [])
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in pool.sql))


if __name__ == "__main__":
    unittest.main()

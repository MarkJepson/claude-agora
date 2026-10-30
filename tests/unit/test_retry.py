"""retry_on_db_error (api/agora_db.py): which DB errors re-run a handler. No
database needed. Run from the repo root with the API's dependencies:

    python3 tests/unit/test_retry.py
"""
import asyncio
import sys
import unittest
from pathlib import Path

_here = Path(__file__).resolve()
for _d in [p / "api" for p in _here.parents] + [Path("/app")]:
    if (_d / "agora_db.py").exists():
        sys.path.insert(0, str(_d))
        break
import pymysql  # noqa: E402

import agora_db  # noqa: E402


def _handler(fail_with: int, failures: int):
    """A handler that raises `fail_with` on its first `failures` calls."""
    calls = []

    @agora_db.retry_on_db_error
    async def handler():
        calls.append(1)
        if len(calls) <= failures:
            raise pymysql.err.OperationalError(fail_with, "simulated")
        return "ok"

    return handler, calls


class Retry(unittest.TestCase):
    def run_handler(self, code, failures):
        handler, calls = _handler(code, failures)
        return asyncio.run(handler()), len(calls)

    def test_write_conflict_1020_is_retried(self):
        self.assertEqual(self.run_handler(1020, 1), ("ok", 2))

    def test_deadlock_is_retried(self):
        self.assertEqual(self.run_handler(1213, 2), ("ok", 3))

    def test_gives_up_after_three_attempts(self):
        handler, calls = _handler(1020, 5)
        with self.assertRaises(pymysql.err.OperationalError):
            asyncio.run(handler())
        self.assertEqual(len(calls), 3)

    def test_lost_connection_is_not_retried(self):
        # The COMMIT may already have succeeded: a retry could post twice.
        handler, calls = _handler(2013, 1)
        with self.assertRaises(pymysql.err.OperationalError):
            asyncio.run(handler())
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

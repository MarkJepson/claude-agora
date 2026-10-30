"""POST /roles/{role}/wait through the REAL app stack (uvicorn, the CSRF
middleware, Starlette). The server can't see a client hang up behind the
middleware, so killed watches' waits stay open; a re-armed watch must still
get a wait (newest wins), not be refused. Needs uvicorn (in the
API's requirements); no database (a fake pool). Run from the repo root:

    python3 tests/unit/test_wait_stack.py
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

_here = Path(__file__).resolve()
for _d in [p / "api" for p in _here.parents] + [Path("/app")]:
    if (_d / "main.py").exists():
        sys.path.insert(0, str(_d))
        break
os.environ.setdefault("DATABASE_URL", "mysql://unused@localhost/unused")
os.environ["RELAY_ALLOWED_HOSTS"] = "127.0.0.1:18980"
import uvicorn  # noqa: E402

import main  # noqa: E402

PORT = 18980


class Conn:
    async def fetchval(self, sql, *a):
        return 1

    async def execute(self, sql, *a):
        return 1

    async def fetch(self, sql, *a):
        return []


class Pool:
    @asynccontextmanager
    async def acquire(self):
        yield Conn()


def wait_request(timeout=25):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/roles/r/wait?timeout={timeout}", data=b'{"known": []}', method="POST",
        headers={"X-Relay-Client": "test", "Content-Type": "application/json"},
    )
    return req


def hang_up_after(seconds):
    """Start a wait and drop the connection after `seconds` (a killed watch)."""
    s = socket.create_connection(("127.0.0.1", PORT))
    body = b'{"known": []}'
    s.sendall(b"POST /roles/r/wait?timeout=25 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Relay-Client: test\r\n"
              b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (PORT, len(body), body))
    time.sleep(seconds)
    s.close()


class Disconnect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main.app.router.on_startup.clear()
        main.pool = Pool()
        cls.server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def test_a_rearmed_watch_gets_in_after_killed_ones(self):
        threads = [threading.Thread(target=hang_up_after, args=(0.5,)) for _ in range(main.WAIT_MAX_PER_ROLE)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Four killed watches' waits are still open and fill every slot.
        self.assertEqual(len(main._waiters.get("r", [])), main.WAIT_MAX_PER_ROLE)
        t = time.monotonic()
        with urllib.request.urlopen(wait_request(timeout=1), timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertFalse(json.loads(resp.read())["superseded"])
        self.assertLess(time.monotonic() - t, 3)


if __name__ == "__main__":
    unittest.main()

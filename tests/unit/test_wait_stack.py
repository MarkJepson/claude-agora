"""POST /roles/{role}/wait through the REAL app stack (uvicorn, the CSRF
middleware, Starlette). The server can't see a client hang up behind the
middleware, so killed watches' waits stay open until their timeout; a
re-armed watch must still get a wait straight away. Needs uvicorn (in the
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
try:
    import uvicorn  # noqa: E402
except ImportError:   # only the API's requirements bring it
    uvicorn = None

import main  # noqa: E402

HANG_UP_TIMEOUT = 2   # the killed watches' waits end this soon, so teardown can wait for them


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


@unittest.skipIf(uvicorn is None, "uvicorn not installed (install api/requirements.txt)")
class Stack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (main.pool, list(main.app.router.on_startup), set(main._ALLOWED_HOSTS))
        main.app.router.on_startup.clear()
        main.pool = Pool()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))   # a free port, whatever else is running
        cls.port = sock.getsockname()[1]
        # Patched directly: the env var is read when main is first imported.
        main._ALLOWED_HOSTS.add(f"127.0.0.1:{cls.port}")
        cls.server = uvicorn.Server(uvicorn.Config(main.app, log_level="warning"))
        threading.Thread(target=cls.server.run, kwargs={"sockets": [sock]}, daemon=True).start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("test server did not start")

    @classmethod
    def tearDownClass(cls):
        # Let the leftover waits finish before restoring what they use.
        for _ in range(100):
            if not main._waiters:
                break
            time.sleep(0.05)
        cls.server.should_exit = True
        main.pool, startup, hosts = cls.saved
        main.app.router.on_startup[:] = startup
        main._ALLOWED_HOSTS.clear()
        main._ALLOWED_HOSTS.update(hosts)

    def hang_up_after(self, seconds):
        """Start a wait and drop the connection (a killed mail watch)."""
        s = socket.create_connection(("127.0.0.1", self.port))
        body = b'{"known": []}'
        s.sendall(b"POST /roles/r/wait?timeout=%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Relay-Client: test\r\n"
                  b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                  % (HANG_UP_TIMEOUT, self.port, len(body), body))
        time.sleep(seconds)
        s.close()

    def test_a_rearmed_watch_gets_in_after_killed_ones(self):
        threads = [threading.Thread(target=self.hang_up_after, args=(0.5,)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # The killed watches' waits are still open (the server can't tell).
        self.assertEqual(len(main._waiters.get("r", ())), 6)
        t = time.monotonic()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/roles/r/wait?timeout=1", data=b'{"known": []}', method="POST",
            headers={"X-Relay-Client": "test", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read()), {"pending": []})
        self.assertLess(time.monotonic() - t, 3)


if __name__ == "__main__":
    unittest.main()

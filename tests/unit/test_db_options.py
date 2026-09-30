"""TLS to the database is OPT-IN. A plain DATABASE_URL must connect without TLS
and without any certificate files (the bundled docker-compose setup relies on
this). No database needed. Run from the repo root with the API's dependencies:

    python3 tests/unit/test_db_options.py
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

_here = Path(__file__).resolve()
for _d in [p / "api" for p in _here.parents] + [Path("/app")]:
    if (_d / "agora_db.py").exists():
        sys.path.insert(0, str(_d))
        break
import aiomysql  # noqa: E402

import agora_db  # noqa: E402


class TlsIsOptional(unittest.TestCase):
    def test_no_tls_params_means_no_ssl_context(self):
        self.assertIsNone(agora_db._ssl_context({}))

    def test_ssl_false_means_no_ssl_context(self):
        self.assertIsNone(agora_db._ssl_context({"ssl": ["false"]}))

    def test_tls_only_when_asked(self):
        self.assertIsNotNone(agora_db._ssl_context({"ssl": ["true"]}))

    def test_plain_url_opens_pool_without_ssl_or_files(self):
        seen = {}

        async def fake_create_pool(**kw):
            seen.update(kw)

            class P:
                pass
            return P()

        real = aiomysql.create_pool
        aiomysql.create_pool = fake_create_pool
        try:
            os.environ.pop("DATABASE_PASSWORD", None)
            pool = asyncio.run(agora_db.create_pool("mysql://agora:pw@db:3306/agora"))
        finally:
            aiomysql.create_pool = real
        self.assertIsNone(seen["ssl"])
        self.assertEqual(pool._watched, [])  # nothing to watch, so no reload logic runs
        self.assertEqual(seen["maxsize"], 4)


if __name__ == "__main__":
    unittest.main()

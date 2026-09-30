"""One-off: move a relay's history from the old PostgreSQL database into a
MariaDB one, keeping every id and timestamp, in two steps so the halves can run
in different places.

    # 1. next to the old database (needs `pip install asyncpg`; read-only):
    SOURCE_DATABASE_URL=postgresql://user:pw@host:5432/agora \\
        python3 pg_migrate.py export dump.json

    # 2. anywhere that can reach the new database (this file ships in the API
    #    image, which already has everything the import needs):
    DATABASE_URL=mysql://user@host:3306/agora DATABASE_PASSWORD=... \\
        python3 pg_migrate.py import dump.json

The dump also records the source's row counts, max ids, message-body bytes and
delivery/answer tallies; `import` compares the copy against them and exits
non-zero unless everything matches, so it needs no access to the source.
`import` applies schema.sql first, and refuses to run if the destination
already holds messages, groups or threads, so it can never merge into or
clobber live data. It runs as one transaction: any failure copies nothing.

Stop the old API before the final `export`, so nothing is posted between the
export and the cutover. The dump contains all message text: treat it as
sensitive and delete it afterwards.
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import agora_db

# (table, columns). FK order. messages.attn_answered_by points forward at a
# later message, so it is filled in a second pass ("links").
TABLES = [
    ("roles", ["role", "repo_path", "created_at", "last_seen_at", "last_seen_action", "last_action_at",
               "heartbeat_at", "heartbeat_interval"]),
    ("role_bindings", ["role", "session_name", "bound_at", "status"]),
    ("groups", ["group_id", "status", "created_at", "created_from"]),
    ("group_members", ["group_id", "role", "joined_at", "left_at"]),
    ("threads", ["thread_id", "group_id", "topic", "status", "created_at", "created_from"]),
    ("messages", ["message_id", "thread_id", "sender_role", "body", "created_at", "attn_kind", "attn_why",
                  "attn_cleared_at"]),
    ("deliveries", ["message_id", "recipient_role", "delivered_at", "read_at", "ack_at"]),
]
ORDER = {"role_bindings": "1", "group_members": "1, 2", "deliveries": "1, 2"}
ID_COLUMN = {"groups": "group_id", "threads": "thread_id", "messages": "message_id"}
FORMAT = 1


class _VerificationFailed(Exception):
    pass


def q(table: str) -> str:
    return f"`{table}`" if table == "groups" else table


def _enc(v):
    return {"$dt": v.isoformat()} if isinstance(v, datetime) else v


def _dec(v):
    return datetime.fromisoformat(v["$dt"]) if isinstance(v, dict) and "$dt" in v else v


async def export(path: str) -> int:
    import asyncpg  # only needed here; not in the API image

    url = os.environ.get("SOURCE_DATABASE_URL")
    if not url:
        print("set SOURCE_DATABASE_URL (postgresql://...)", file=sys.stderr)
        return 2
    src = await asyncpg.connect(url)
    try:
        # One repeatable-read snapshot for every query, so the tables and the
        # recorded checks agree even if the old API is still being written to.
        async with src.transaction(isolation="repeatable_read", readonly=True):
            return await _export_snapshot(src, path)
    finally:
        await src.close()


async def _export_snapshot(src, path: str) -> int:
    dump = {"format": FORMAT, "tables": {}, "links": [], "checks": {}}
    for table, cols in TABLES:
        rows = await src.fetch(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {ORDER.get(table, cols[0])}")
        dump["tables"][table] = [[_enc(r[c]) for c in cols] for r in rows]
        print(f"exported {table}: {len(rows)} rows")
    links = await src.fetch("SELECT message_id, attn_answered_by FROM messages WHERE attn_answered_by IS NOT NULL")
    dump["links"] = [[r["message_id"], r["attn_answered_by"]] for r in links]
    checks = {"counts": {t: len(dump["tables"][t]) for t, _ in TABLES}, "max_ids": {}}
    for table, col in ID_COLUMN.items():
        checks["max_ids"][table] = await src.fetchval(f"SELECT COALESCE(MAX({col}), 0) FROM {table}")
    checks["body_bytes"] = int(await src.fetchval("SELECT COALESCE(SUM(octet_length(body)), 0) FROM messages"))
    checks["delivery_tallies"] = [
        await src.fetchval(f"SELECT COUNT(*) FROM deliveries WHERE {c} IS NOT NULL")
        for c in ("delivered_at", "read_at", "ack_at")
    ]
    checks["answered_asks"] = len(dump["links"])
    dump["checks"] = checks
    Path(path).write_text(json.dumps(dump, ensure_ascii=False))
    os.chmod(path, 0o600)
    print(f"wrote {path} (mode 600). It holds all message text: delete it when the import is done.")
    return 0


async def import_(path: str) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("set DATABASE_URL (mysql://...)", file=sys.stderr)
        return 2
    dump = json.loads(Path(path).read_text())
    if dump.get("format") != FORMAT:
        print(f"unsupported dump format {dump.get('format')!r}", file=sys.stderr)
        return 2
    pool = await agora_db.create_pool(url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as dst:
            await agora_db.apply_schema(dst, Path(__file__).resolve().parent / "schema.sql")
            for t in ("messages", "groups", "threads"):
                if await dst.fetchval(f"SELECT COUNT(*) FROM {q(t)}"):
                    print(f"refusing: destination already has rows in {t}; import into an empty database", file=sys.stderr)
                    return 1
            failures: list[str] = []
            try:
                async with dst.transaction():
                    for table, cols in TABLES:
                        ph = agora_db.ph(1, len(cols))
                        if table == "roles":
                            # 'operator' is seeded by schema.sql; take the source's values for it too.
                            sql = (f"INSERT INTO roles ({', '.join(cols)}) VALUES ({ph}) ON DUPLICATE KEY UPDATE "
                                   + ", ".join(f"{c} = VALUES({c})" for c in cols[1:]))
                        else:
                            sql = f"INSERT INTO {q(table)} ({', '.join(cols)}) VALUES ({ph})"
                        for row in dump["tables"][table]:
                            await dst.execute(sql, *[_dec(v) for v in row])
                        print(f"imported {table}: {len(dump['tables'][table])} rows")
                    for message_id, answered_by in dump["links"]:
                        await dst.execute("UPDATE messages SET attn_answered_by = $1 WHERE message_id = $2",
                                          answered_by, message_id)
                    print(f"linked attn_answered_by: {len(dump['links'])}")

                    # ---- verify against the source as it was at export time, BEFORE
                    # committing: any mismatch rolls the whole import back ----
                    chk = dump["checks"]

                    def report(good: bool, line: str):
                        print(("OK   " if good else "FAIL ") + line)
                        if not good:
                            failures.append(line)

                    for table, _ in TABLES:
                        n = await dst.fetchval(f"SELECT COUNT(*) FROM {q(table)}")
                        report(n == chk["counts"][table], f"{table}: dump {chk['counts'][table]}, database {n}")
                    for table, col in ID_COLUMN.items():
                        mx = await dst.fetchval(f"SELECT COALESCE(MAX({col}), 0) FROM {q(table)}")
                        report(mx == chk["max_ids"][table], f"max {col}: dump {chk['max_ids'][table]}, database {mx}")
                    body = int(await dst.fetchval("SELECT COALESCE(SUM(OCTET_LENGTH(body)), 0) FROM messages"))
                    report(body == chk["body_bytes"], f"message body bytes: dump {chk['body_bytes']}, database {body}")
                    tallies = [await dst.fetchval(f"SELECT COUNT(*) FROM deliveries WHERE {c} IS NOT NULL")
                               for c in ("delivered_at", "read_at", "ack_at")]
                    report(tallies == chk["delivery_tallies"],
                           f"deliveries delivered/read/ack: dump {chk['delivery_tallies']}, database {tallies}")
                    answered = await dst.fetchval("SELECT COUNT(*) FROM messages WHERE attn_answered_by IS NOT NULL")
                    report(answered == chk["answered_asks"], f"answered asks: dump {chk['answered_asks']}, database {answered}")
                    if failures:
                        raise _VerificationFailed()
            except _VerificationFailed:
                print("IMPORT FAILED VERIFICATION: rolled back, nothing was imported", file=sys.stderr)
                return 1
            print("IMPORT VERIFIED")
            return 0
    finally:
        await pool.close()


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "import"):
        print(__doc__, file=sys.stderr)
        return 2
    return asyncio.run(export(sys.argv[2]) if sys.argv[1] == "export" else import_(sys.argv[2]))


if __name__ == "__main__":
    sys.exit(main())

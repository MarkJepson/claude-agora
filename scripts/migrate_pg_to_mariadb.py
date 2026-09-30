#!/usr/bin/env python3
"""One-off: copy a relay's history from the old PostgreSQL database into a
MariaDB one, keeping every id and timestamp, then verify the copy.

    SOURCE_DATABASE_URL=postgresql://user:pw@host:5432/agora \\
    DATABASE_URL=mysql://user:pw@host:3306/agora \\
    python3 scripts/migrate_pg_to_mariadb.py [--replace-seed-only]

Needs (in a throwaway venv, not the API image):  pip install asyncpg aiomysql PyMySQL

Safe by construction:
  * the source is only read;
  * it refuses to run if the destination already holds messages or groups, so
    it can never merge into or clobber live data;
  * it applies api/schema.sql to the destination first (idempotent), so the
    destination can be a brand-new empty database;
  * ids are copied as-is (AUTO_INCREMENT then continues after the max);
  * it exits non-zero unless every table's row count and max id match, plus a
    content checksum on messages.

Stop the source relay's writers (or the source API) before the final run, so
nothing is posted between the copy and the cutover. Run it against a scratch
MariaDB first to rehearse.
"""
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
import agora_db  # noqa: E402

BATCH = 500

# (table, columns, id column for the max check). FK order; messages is copied
# in two passes because attn_answered_by points forward at a later message.
TABLES = [
    ("roles", ["role", "repo_path", "created_at", "last_seen_at", "last_seen_action", "last_action_at",
               "heartbeat_at", "heartbeat_interval"], None),
    ("role_bindings", ["role", "session_name", "bound_at", "status"], None),
    ("groups", ["group_id", "status", "created_at", "created_from"], "group_id"),
    ("group_members", ["group_id", "role", "joined_at", "left_at"], None),
    ("threads", ["thread_id", "group_id", "topic", "status", "created_at", "created_from"], "thread_id"),
    ("messages", ["message_id", "thread_id", "sender_role", "body", "created_at", "attn_kind", "attn_why",
                  "attn_cleared_at"], "message_id"),
    ("deliveries", ["message_id", "recipient_role", "delivered_at", "read_at", "ack_at"], None),
]


def q(name: str) -> str:
    return f"`{name}`"


async def main() -> int:
    src_url, dst_url = os.environ.get("SOURCE_DATABASE_URL"), os.environ.get("DATABASE_URL")
    if not src_url or not dst_url:
        print("set SOURCE_DATABASE_URL (postgresql://...) and DATABASE_URL (mysql://...)", file=sys.stderr)
        return 2
    src = await asyncpg.connect(src_url)
    pool = await agora_db.create_pool(dst_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as dst:
            await agora_db.apply_schema(dst, Path(__file__).resolve().parent.parent / "api" / "schema.sql")
            for t in ("messages", "`groups`", "threads"):
                if await dst.fetchval(f"SELECT COUNT(*) FROM {t}"):
                    print(f"refusing: destination already has rows in {t}; migrate into an empty database", file=sys.stderr)
                    return 1

            async with dst.transaction():
                for table, cols, _ in TABLES:
                    rows = await src.fetch(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1"
                                           + (", 2" if table in ("group_members", "deliveries", "role_bindings") else ""))
                    ph = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
                    if table == "roles":
                        # 'operator' is seeded by schema.sql; take the source's values for it too.
                        sql = (f"INSERT INTO roles ({', '.join(cols)}) VALUES ({ph}) ON DUPLICATE KEY UPDATE "
                               + ", ".join(f"{c} = VALUES({c})" for c in cols[1:]))
                    else:
                        sql = f"INSERT INTO {q(table)} ({', '.join(cols)}) VALUES ({ph})"
                    for r in rows:
                        await dst.execute(sql, *[r[c] for c in cols])
                    print(f"copied {table}: {len(rows)} rows")

                # Second pass: messages.attn_answered_by, now that every message exists.
                links = await src.fetch("SELECT message_id, attn_answered_by FROM messages WHERE attn_answered_by IS NOT NULL")
                for r in links:
                    await dst.execute("UPDATE messages SET attn_answered_by = $1 WHERE message_id = $2",
                                      r["attn_answered_by"], r["message_id"])
                print(f"linked attn_answered_by: {len(links)}")

            # ---- verify ----
            ok = True
            for table, _, idcol in TABLES:
                a = await src.fetchval(f"SELECT COUNT(*) FROM {table}")
                b = await dst.fetchval(f"SELECT COUNT(*) FROM {q(table)}")
                line = f"{table}: source {a}, destination {b}"
                if idcol:
                    ma = await src.fetchval(f"SELECT COALESCE(MAX({idcol}), 0) FROM {table}")
                    mb = await dst.fetchval(f"SELECT COALESCE(MAX({idcol}), 0) FROM {q(table)}")
                    line += f", max {idcol} {ma}/{mb}"
                    ok &= ma == mb
                ok &= a == b
                print(("OK   " if a == b else "FAIL ") + line)
            # content checksum: total body bytes and attention/ack/read tallies
            sa = await src.fetchrow("SELECT COALESCE(SUM(octet_length(body)), 0) AS n FROM messages")
            sb = await dst.fetchrow("SELECT COALESCE(SUM(OCTET_LENGTH(body)), 0) AS n FROM messages")
            same_bytes = int(sa["n"]) == int(sb["n"])
            ta = [await src.fetchval(f"SELECT COUNT(*) FROM deliveries WHERE {c} IS NOT NULL") for c in ("delivered_at", "read_at", "ack_at")]
            tb = [await dst.fetchval(f"SELECT COUNT(*) FROM deliveries WHERE {c} IS NOT NULL") for c in ("delivered_at", "read_at", "ack_at")]
            aa = await src.fetchval("SELECT COUNT(*) FROM messages WHERE attn_answered_by IS NOT NULL")
            ab = await dst.fetchval("SELECT COUNT(*) FROM messages WHERE attn_answered_by IS NOT NULL")
            print(("OK   " if same_bytes else "FAIL ") + f"message body bytes: source {sa['n']}, destination {sb['n']}")
            print(("OK   " if ta == tb else "FAIL ") + f"deliveries delivered/read/ack: source {ta}, destination {tb}")
            print(("OK   " if aa == ab else "FAIL ") + f"answered asks: source {aa}, destination {ab}")
            ok &= same_bytes and ta == tb and aa == ab
            print("MIGRATION VERIFIED" if ok else "MIGRATION FAILED VERIFICATION")
            return 0 if ok else 1
    finally:
        await src.close()
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

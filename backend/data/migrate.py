"""Migrations: numbered SQL files in backend/data/migrations, the same files Wrangler applies.

Production: `wrangler d1 migrations apply leaf-platform --remote` (run by
scripts/deploy_cloudflare.sh before every deploy). Locally and in tests: `apply()` below,
which records each file in Wrangler's own `d1_migrations` table, so both tools agree on
what has run. The container never migrates D1 itself; it only checks the schema is current.

    .venv/bin/python -m backend.data.migrate [path/to/platform.db]
"""

from __future__ import annotations

import sys
from pathlib import Path

from backend.data.driver import Driver, SqliteDriver, query

MIGRATIONS = Path(__file__).resolve().parent / "migrations"

# Wrangler's table, created the same way so either tool can apply the next file.
TRACKING = """CREATE TABLE IF NOT EXISTS d1_migrations(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
)"""


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))


async def applied(db: Driver) -> list[str]:
    rows = await query(
        db, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'd1_migrations'"
    )
    if not rows:
        return []
    return [r["name"] for r in await query(db, "SELECT name FROM d1_migrations ORDER BY id")]


async def pending(db: Driver) -> list[str]:
    done = set(await applied(db))
    return [f.name for f in migration_files() if f.name not in done]


async def apply(db: SqliteDriver) -> list[str]:
    """Apply every pending migration to a SQLite database, one transaction per file."""
    await db.batch([(TRACKING, ())])
    names = await pending(db)
    for name in names:
        sql = (MIGRATIONS / name).read_text()
        escaped = name.replace("'", "''")
        db.executescript(f"{sql}\nINSERT INTO d1_migrations (name) VALUES ('{escaped}');")
    return names


if __name__ == "__main__":  # pragma: no cover - dev helper
    import asyncio

    target = sys.argv[1] if len(sys.argv) > 1 else "data/platform.db"
    done = asyncio.run(apply(SqliteDriver(target)))
    print(f"{target}: applied {done or 'nothing (up to date)'}")

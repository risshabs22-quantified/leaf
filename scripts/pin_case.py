"""Keep a journal entry (a share link) alive past the usual 30 days, e.g. for the Devpost demo.

    python scripts/pin_case.py <case_id> --until 2027-01-31

Uses the configured store (LEAF_JOURNAL_BUCKET in production, else LEAF_JOURNAL_PATH), so run
it with the same settings as the server. Pin before the entry expires: an expired entry is gone.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.config import get_settings  # noqa: E402
from backend.journal import open_journal  # noqa: E402


async def pin(case_id: str, until: datetime) -> int:
    s = get_settings()
    store = open_journal(s.journal_path, s.journal_bucket)
    entry = await store.get(case_id)
    if entry is None:
        print(f"No live entry {case_id!r} in this store (it may have expired).")
        return 1
    entry.expires = max(entry.expires, until.timestamp())
    await store.put(entry)
    print(f"{case_id} now kept until {datetime.fromtimestamp(entry.expires, UTC):%Y-%m-%d}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("case_id")
    ap.add_argument("--until", required=True, help="YYYY-MM-DD (UTC, end of day)")
    args = ap.parse_args()
    until = datetime.strptime(args.until, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=UTC
    )
    return asyncio.run(pin(args.case_id, until))


if __name__ == "__main__":
    sys.exit(main())

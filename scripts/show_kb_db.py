"""Demo helper — prove KB articles are really persisted in the database.

Reads the raw SQLite file directly (NOT through the app), so it demonstrates
that the Knowledge Synthesizer's output is durably stored, not just shown in
the UI. Run it before and after synthesizing on the Knowledge page.

    uv run python scripts/show_kb_db.py

Set AIOPS_STATE_DB_URL / a custom path with --db if you moved the database.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

COLS = [
    "id",
    "status",
    "incident_id",
    "service",
    "quality_score",
    "related_runbook_id",
    "title",
    "created_at",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/state.db", help="path to the SQLite state DB")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"✗ no database at {db} — run the app / synthesize an article first.")
        return

    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            f"SELECT {', '.join(COLS)} FROM kb_articles ORDER BY created_at DESC"
        ).fetchall()
    finally:
        con.close()

    print(f"\nDatabase : {db.resolve()}")
    print(f"Table    : kb_articles   ({len(rows)} row{'s' if len(rows) != 1 else ''})\n")
    for r in rows:
        d = dict(zip(COLS, r, strict=False))
        flag = "[PUBLISHED]" if d["status"] == "published" else f"[{str(d['status']).upper()}]"
        print(f"  #{d['id']}  {flag}")
        print(f"      incident   : {d['incident_id']}")
        print(
            f"      service    : {d['service']}   quality={d['quality_score']}   runbook={d['related_runbook_id']}"
        )
        print(f"      title      : {d['title']}")
        print(f"      created_at : {d['created_at']}\n")


if __name__ == "__main__":
    main()

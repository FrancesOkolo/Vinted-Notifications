"""One-off import of six skincare searches into the existing local Vinted app.

From the repository root:
    python scripts/add_skincare_queries.py
    python scripts/add_skincare_queries.py --apply

Default is read-only. Import is idempotent; it preserves existing searches,
item history, query preferences, monitoring cadence and other settings.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from url_normalizer import normalise_vinted_url  # noqa: E402

SKINCARE_SEARCHES = (
    ("Medik8 Crystal Retinal Ceramide Eye", "Medik8 Crystal Retinal Ceramide Eye"),
    ("Medik8 Crystal Retinal 5", "Medik8 Crystal Retinal 5"),
    ("SkinCeuticals Best Sellers Discovery Set", "SkinCeuticals Discovery Set"),
    ("ZO Skin Health Exfoliating Polish", "ZO Exfoliating Polish"),
    ("Skinbetter Science AlphaRet Exfoliating Peel Pads", "AlphaRet Exfoliating Peel Pads"),
    ("Skinbetter Science AlphaRet Overnight Cream", "AlphaRet Overnight Cream"),
)


def _url(terms: str) -> str:
    return normalise_vinted_url(
        "https://www.vinted.co.uk/catalog?search_text=" + quote(terms)
    )


def _admin_chat_id(conn: sqlite3.Connection, selected: str | None) -> str:
    if selected:
        approved = conn.execute(
            "SELECT 1 FROM telegram_users WHERE chat_id=? AND status='approved'",
            (selected,),
        ).fetchone()
        if not approved:
            raise ValueError("--chat-id must identify an approved Telegram user")
        return selected

    # Use the existing configured recipient when it is an approved account.
    row = conn.execute(
        "SELECT value FROM parameters WHERE key='telegram_chat_id'"
    ).fetchone()
    configured = str(row[0]).strip() if row and row[0] else ""
    if configured and conn.execute(
        "SELECT 1 FROM telegram_users WHERE chat_id=? AND status='approved'",
        (configured,),
    ).fetchone():
        return configured

    admins = conn.execute(
        "SELECT chat_id FROM telegram_users WHERE status='approved' AND is_admin=1"
    ).fetchall()
    if len(admins) != 1:
        raise ValueError(
            "Could not identify exactly one approved Telegram admin. "
            "Pass --chat-id for an approved account, or use the Web UI."
        )
    return str(admins[0][0])


def import_searches(db_path: Path, *, apply: bool, chat_id: str | None = None) -> None:
    if not db_path.is_file():
        raise FileNotFoundError(f"Existing app database not found: {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        for table in ("queries", "query_preferences", "query_subscriptions", "telegram_users", "parameters"):
            if not conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                raise ValueError(f"Missing app table {table}; run the app to finish migrations")
        recipient = _admin_chat_id(conn, chat_id)

        existing = {}
        for query_id, existing_url in conn.execute("SELECT id, query FROM queries"):
            try:
                canonical = normalise_vinted_url(existing_url)
            except (ValueError, TypeError):
                canonical = existing_url
            existing.setdefault(canonical, query_id)

        plan = [(name, _url(terms), existing.get(_url(terms))) for name, terms in SKINCARE_SEARCHES]
        print(f"Database: {db_path}")
        for name, _, query_id in plan:
            print(f"{'Already exists' if query_id else 'Add (normal)'}: {name}")
        if not apply:
            print("Dry run only: nothing changed. Re-run with --apply to add searches.")
            return

        # SQLite online backup is safe even while the app is running in WAL mode.
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = db_path.with_name(f"{db_path.name}.pre-skincare-{stamp}.bak")
        with sqlite3.connect(backup) as backup_conn:
            conn.backup(backup_conn)
        print(f"SQLite backup created: {backup}")

        added = subscribed = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, url, existing_id in plan:
                if existing_id is None:
                    conn.execute(
                        "INSERT INTO queries(query, last_item, query_name) VALUES (?, NULL, ?)",
                        (url, name),
                    )
                    query_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        "INSERT OR IGNORE INTO query_preferences(query_id) VALUES (?)",
                        (query_id,),
                    )
                    added += 1
                else:
                    query_id = existing_id

                cursor = conn.execute(
                    "INSERT OR IGNORE INTO query_subscriptions(query_id, chat_id) VALUES (?, ?)",
                    (query_id, recipient),
                )
                subscribed += cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"Completed: {added} new searches; {subscribed} new Telegram subscriptions.")
        print("Existing searches, settings, polling tiers and item history were not altered.")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Import into the local app database")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "vinted_notifications.db")
    parser.add_argument("--chat-id", help="Approved Telegram chat ID, only when not auto-detected")
    args = parser.parse_args()
    try:
        import_searches(args.db.resolve(), apply=args.apply, chat_id=args.chat_id)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Import stopped without changing searches: {exc}\n")


if __name__ == "__main__":
    main()

"""Read-only reconciliation between the actual mailbox and processing records."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import sqlite3
from .mail import IMAPClient, request_mode
from .security import rejection


def _read_records(config):
    records = {}
    saved_since = None
    if config.database.exists():
        db = sqlite3.connect(config.database.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            records = {r["message_id"]: dict(r) for r in db.execute(
                "SELECT message_id,state,error_id,created_at FROM messages WHERE account=?", (config.email,))}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
                saved = db.execute("SELECT value FROM settings WHERE account=? AND name='scan_start'", (config.email,)).fetchone()
                if saved:
                    saved_since = saved[0]
        finally:
            db.close()
    return records, saved_since


def inspect_status(config, mailbox_factory=IMAPClient):
    records, saved_since = _read_records(config)
    since = config.imap_start_date or saved_since
    if not since:
        first = min((r["created_at"] for r in records.values()), default=datetime.now(timezone.utc).timestamp())
        since = (datetime.fromtimestamp(first, timezone.utc) - timedelta(days=1)).date().isoformat()
    with mailbox_factory(config, since=since) as mailbox:
        candidates = list(mailbox.candidates())
    # Mail retrieval can take seconds. Refresh after it, using one short database
    # snapshot, so messages handled during the scan are not falsely shown pending.
    records, _ = _read_records(config)
    received = []
    for mail in candidates:
        if mail.sender not in config.allowed:
            continue
        reason = rejection(mail, config) or (None if request_mode(mail, config) else "subject")
        row = records.get(mail.message_id)
        state = row["state"] if row else ("filtered" if reason else "pending-not-recorded")
        received.append({"message_id": mail.message_id, "subject": mail.subject,
                         "state": state, "filter_reason": reason,
                         "error_id": row["error_id"] if row else None})
    return {"checked_at": datetime.now().astimezone().isoformat(), "scan_since": since,
            "database_counts": dict(Counter(r["state"] for r in records.values())),
            "mailbox_counts": dict(Counter(r["state"] for r in received)), "messages": received}

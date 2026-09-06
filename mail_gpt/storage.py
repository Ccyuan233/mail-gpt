"""Durable inbox/outbox state. External effects are never blindly retried."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone


@contextmanager
def instance_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = open(path, "a+b")
    locked = False
    try:
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        if os.fstat(file.fileno()).st_size == 0:
            file.write(b"0")
            file.flush()
        yield
    finally:
        if locked:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS conversations (
                thread_key TEXT PRIMARY KEY, session_id TEXT,
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS thread_aliases (
                account TEXT NOT NULL, sender TEXT NOT NULL, alias TEXT NOT NULL,
                thread_key TEXT NOT NULL,
                PRIMARY KEY(account, sender, alias));
            CREATE TABLE IF NOT EXISTS messages (
                account TEXT NOT NULL, message_id TEXT NOT NULL,
                thread_key TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
                state TEXT NOT NULL, reply_id TEXT NOT NULL, reply BLOB,
                error_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(account, message_id));
            CREATE TABLE IF NOT EXISTS settings (
                account TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(account, name));
        """)

    def close(self):
        self.db.close()

    def scan_start(self, account, override=""):
        saved = self.db.execute("SELECT value FROM settings WHERE account=? AND name='scan_start'", (account,)).fetchone()
        earliest = self.db.execute("SELECT MIN(created_at) FROM messages WHERE account=?", (account,)).fetchone()[0]
        value = override or (saved[0] if saved else (
            datetime.fromtimestamp(earliest if earliest is not None else time.time(), timezone.utc)
            - timedelta(days=1)).date().isoformat())
        with self.db:
            self.db.execute("INSERT INTO settings VALUES (?,'scan_start',?) ON CONFLICT(account,name) DO UPDATE SET value=excluded.value", (account, value))
        return value

    def backfill_gmail_aliases(self, account, mails):
        """Reconnect pre-fix sessions without changing any existing Gmail mapping.

        Legacy duplicate Gmail conversations use their most recently answered session.
        Historical RFC aliases and the older session files remain intact.
        """
        latest = {}
        for mail in mails:
            if not mail.gmail_thread:
                continue
            row = self.message(account, mail.message_id)
            if not row or row["sender"] != mail.sender or row["state"] != "sent":
                continue
            key = (mail.sender, "gmail:" + mail.gmail_thread)
            if key not in latest or row["created_at"] > latest[key]["created_at"]:
                latest[key] = row
        with self.db:
            for (sender, alias), row in latest.items():
                self.db.execute("INSERT OR IGNORE INTO thread_aliases VALUES (?,?,?,?)",
                                (account, sender, alias, row["thread_key"]))

    def recover(self):
        # Caller holds the process lock: these effects belong to a stopped previous run.
        with self.db:
            self.db.execute("UPDATE conversations SET blocked=1 WHERE thread_key IN "
                            "(SELECT thread_key FROM messages WHERE state='generating')")
            self.db.execute("UPDATE messages SET state='review', error_id=COALESCE(error_id, 'interrupted') "
                            "WHERE state IN ('generating','sending')")

    def message(self, account, message_id):
        return self.db.execute("SELECT * FROM messages WHERE account=? AND message_id=?",
                               (account, message_id)).fetchone()

    def resolve(self, account, mail):
        aliases = (["gmail:" + mail.gmail_thread] if mail.gmail_thread else [])
        # RFC references also link outgoing replies; this survives Gmail subject re-grouping.
        aliases += ["rfc:" + v for v in mail.in_reply_to + list(reversed(mail.references))]
        for alias in aliases:
            row = self.db.execute("SELECT thread_key FROM thread_aliases WHERE account=? AND sender=? AND alias=?",
                                  (account, mail.sender, alias)).fetchone()
            if row:
                return row[0]
        return str(uuid.uuid4())

    def claim(self, account, mail):
        existing = self.message(account, mail.message_id)
        if existing:
            return existing, False
        key = self.resolve(account, mail)
        now = time.time()
        reply_id = f"<mail-gpt.{uuid.uuid4().hex}@{account.split('@')[1]}>"
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO conversations VALUES (?,NULL,0,?,?)", (key, now, now))
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,'pending',?,NULL,NULL,?,?)",
                            (account, mail.message_id, key, mail.sender, mail.subject, reply_id, now, now))
            aliases = ["rfc:" + mail.message_id, "rfc:" + reply_id]
            if mail.gmail_thread:
                aliases.append("gmail:" + mail.gmail_thread)
            for alias in aliases:
                self.db.execute("INSERT OR IGNORE INTO thread_aliases VALUES (?,?,?,?)",
                                (account, mail.sender, alias, key))
        return self.message(account, mail.message_id), True

    def conversation(self, key):
        return self.db.execute("SELECT * FROM conversations WHERE thread_key=?", (key,)).fetchone()

    def busy(self, row):
        return self.db.execute("SELECT 1 FROM messages WHERE thread_key=? AND message_id<>? "
                               "AND state IN ('pending','generating','ready','sending','review') LIMIT 1",
                               (row['thread_key'], row['message_id'])).fetchone() is not None

    def count_recent(self, account):
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE account=? AND created_at>?",
                               (account, time.time() - 3600)).fetchone()[0]

    def state(self, account, mid, state, error_id=None):
        with self.db:
            self.db.execute("UPDATE messages SET state=?,error_id=?,updated_at=? WHERE account=? AND message_id=?",
                            (state, error_id, time.time(), account, mid))

    def session(self, key, session_id):
        with self.db:
            self.db.execute("UPDATE conversations SET session_id=?,blocked=0,updated_at=? WHERE thread_key=?",
                            (session_id, time.time(), key))

    def block(self, key):
        with self.db:
            self.db.execute("UPDATE conversations SET blocked=1 WHERE thread_key=?", (key,))

    def ready(self, account, mid, raw, error_id=None):
        with self.db:
            self.db.execute("UPDATE messages SET reply=?,state='ready',error_id=?,updated_at=? "
                            "WHERE account=? AND message_id=?", (raw, error_id, time.time(), account, mid))

    def ready_messages(self, account):
        return self.db.execute("SELECT * FROM messages WHERE account=? AND state='ready' ORDER BY created_at",
                               (account,)).fetchall()

    def review(self):
        return self.db.execute("SELECT account,message_id,thread_key,state,error_id,reply_id "
                               "FROM messages WHERE state='review' ORDER BY created_at").fetchall()

    def resolve_review(self, account, mid, action):
        row = self.message(account, mid)
        if not row or row["state"] != "review":
            raise ValueError("Message is not awaiting review")
        if action == "retry-send" and row["reply"] is None:
            raise ValueError("No saved reply; skip this message and send a new [GPT:NEW] request")
        self.state(account, mid, {"retry-send": "ready", "sent": "sent", "skip": "skipped"}[action])

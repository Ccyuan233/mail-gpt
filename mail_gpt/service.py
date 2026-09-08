import json
import logging
import time
import uuid
from .mail import request_mode, make_reply
from .security import rejection
from .runtime import is_paused

log = logging.getLogger("mail_gpt")


class Service:
    def __init__(self, config, store, backend, smtp, health=None):
        self.config, self.store, self.backend, self.smtp = config, store, backend, smtp
        self.health = health

    def deliver(self, row):
        c, db = self.config, self.store
        if row["sender"] not in c.allowed or row["sender"] == c.email:
            db.state(c.email, row["message_id"], "review", "sender-no-longer-allowed")
            return "review"
        # Commit before sending: any interruption from this point is uncertain delivery.
        if self.health:
            self.health.beat("sending")
        db.state(c.email, row["message_id"], "sending", row["error_id"])
        try:
            self.smtp.send(row["sender"], bytes(row["reply"]))
        except Exception as exc:
            error = uuid.uuid4().hex[:12]
            db.state(c.email, row["message_id"], "review", error)
            log.error("smtp_review error_id=%s category=%s", error, type(exc).__name__)
            return "review"
        db.state(c.email, row["message_id"], "sent", row["error_id"])
        return "sent"

    def flush_outbox(self):
        for row in self.store.ready_messages(self.config.email):
            if self.health and is_paused(self.config):
                break
            self.deliver(row)

    def process(self, mail):
        start = time.monotonic()
        c, db = self.config, self.store
        mode = request_mode(mail, c)
        if rejection(mail, c) or not mode:
            return "ignored"
        row = db.message(c.email, mail.message_id)
        if row:
            if row["sender"] != mail.sender:
                return "ignored"
            if row["state"] == "ready":
                return self.deliver(row)
            if row["state"] == "sent":
                return "already-sent"
            if row["state"] != "pending":
                return row["state"]
        elif db.count_recent(c.email) >= c.max_requests_per_hour:
            return "rate-limited"
        # Do not create a pending row behind a previous unfinished request.
        key = row["thread_key"] if row else db.resolve(c.email, mail)
        probe = {"thread_key": key, "message_id": mail.message_id}
        if db.busy(probe):
            return "waiting"
        if not row:
            row, _ = db.claim(c.email, mail)
        conv = db.conversation(row["thread_key"])
        error = None
        if not mail.body or len(mail.body) > c.max_prompt_chars:
            answer = "Please send a non-empty plain-text question within the configured size limit. Attachments are ignored."
        elif conv["blocked"] and mode != "new":
            answer = "The previous AI turn was interrupted. Send a new message with subject [GPT:NEW] to start fresh."
        else:
            if self.health:
                self.health.beat("generating")
            db.state(c.email, mail.message_id, "generating")
            try:
                answer = self.backend.generate(mail.subject, mail.body,
                    None if mode == "new" else conv["session_id"],
                    lambda sid: db.session(row["thread_key"], sid))
            except Exception as exc:
                error = uuid.uuid4().hex[:12]
                db.block(row["thread_key"])
                answer = ("The AI backend failed to process this request.\nError ID: " + error
                          + "\nSend a new [GPT:NEW] message after the backend has been checked.")
                log.error("backend_failed error_id=%s category=%s", error, type(exc).__name__)
        raw = make_reply(mail, c.email, answer, row["reply_id"])
        db.ready(c.email, mail.message_id, raw, error)
        state = self.deliver(db.message(c.email, mail.message_id))
        log.info(json.dumps({"message_id": mail.message_id, "thread_id": mail.gmail_thread or row["thread_key"],
                             "sender": mail.sender, "subject": mail.subject[:800],
                             "codex_session_id": db.conversation(row["thread_key"])["session_id"],
                             "status": state, "duration": round(time.monotonic() - start, 3)}, ensure_ascii=True))
        return state

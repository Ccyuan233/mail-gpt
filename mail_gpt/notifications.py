"""Persistent, coalesced fault notices. Only the supervisor writes this queue."""
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import formatdate
import logging
import time
import uuid
from .mail import SMTPNotSubmitted
from .runtime import read_json, write_json

log = logging.getLogger("mail_gpt")


class Notifications:
    def __init__(self, config, smtp, clock=time.time):
        self.config, self.smtp, self.clock = config, smtp, clock
        self.path = config.runtime / "notifications.json"
        self.data = read_json(self.path, {"active": {}, "events": [], "next_attempt": 0})
        # A previous supervisor may have died after submission. Never blind-resend.
        for event in self.data["events"]:
            if event["state"] == "sending":
                event["state"] = "uncertain"
        self.save()

    def save(self):
        # Retain pending notices and the most recent completed history.
        pending = [e for e in self.data["events"] if e["state"] == "pending"]
        history = [e for e in self.data["events"] if e["state"] != "pending"][-100:]
        self.data["events"] = sorted(pending + history, key=lambda e: e["created_at"])
        write_json(self.path, self.data)

    def issue(self, kind, fingerprint, text):
        current = self.data["active"].get(kind)
        if current and current["fingerprint"] == fingerprint:
            return
        event = {"id": uuid.uuid4().hex, "kind": kind, "text": text,
                 "state": "pending", "created_at": self.clock()}
        self.data["events"].append(event)
        self.data["active"][kind] = {"fingerprint": fingerprint, "event_id": event["id"]}
        self.save()

    def clear(self, kind, text):
        current = self.data["active"].pop(kind, None)
        if not current:
            return
        event = next((e for e in self.data["events"] if e["id"] == current["event_id"]), None)
        if event and event["state"] == "pending":
            event["text"] += "\n\n后续状态：" + text
        elif event and event["state"] == "sent":
            self.data["events"].append({"id": uuid.uuid4().hex, "kind": "recovered",
                "text": text, "state": "pending", "created_at": self.clock()})
        self.save()

    def flush(self):
        c, now = self.config, self.clock()
        if not c.notify_email or now < self.data["next_attempt"]:
            return
        if c.notify_email not in c.allowed or c.notify_email == c.email:
            raise ValueError("Notification recipient is no longer authorized")
        pending = [e for e in self.data["events"] if e["state"] == "pending"]
        if not pending:
            return
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"], msg["To"] = c.email, c.notify_email
        msg["Subject"] = "邮件机器人运行提醒"
        msg["Message-ID"] = f"<mail-gpt-notice.{pending[0]['id']}@{c.email.split('@')[1]}>"
        msg["Date"] = formatdate()
        msg["Auto-Submitted"], msg["X-Auto-Response-Suppress"], msg["X-Mail-GPT"] = "auto-generated", "All", "1"
        body = ["这是邮件机器人的运行通知。"]
        for event in pending:
            when = datetime.fromtimestamp(event["created_at"], timezone.utc).astimezone().isoformat(timespec="seconds")
            body.append(when + "\n" + event["text"])
            event.update(state="sending", last_attempt=now)
        self.data["next_attempt"] = now + 300
        self.save()
        msg.set_content("\n\n".join(body) + "\n\n电脑完全关机或断网时无法发出提醒；确认尚未提交的通知会保留到网络恢复后发送。")
        try:
            self.smtp.send(c.notify_email, msg.as_bytes())
        except SMTPNotSubmitted:
            for event in pending:
                event["state"] = "pending"
            log.warning("notification_connection_failed will_retry=true")
        except Exception as exc:
            for event in pending:
                event["state"] = "uncertain"
            log.error("notification_delivery_uncertain category=%s", type(exc).__name__)
        else:
            for event in pending:
                event["state"] = "sent"
        self.save()

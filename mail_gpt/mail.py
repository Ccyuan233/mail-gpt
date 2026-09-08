"""Provider boundary, MIME parsing, safe reply construction, IMAP and SMTP."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from email.utils import getaddresses, formatdate
from html.parser import HTMLParser
import imaplib
import re
import smtplib
import ssl
from typing import Protocol
from .runtime import read_json

ID = re.compile(r"<[^<>\s]+@[^<>\s]+>")


def message_ids(value):
    return ID.findall(value or "")


def base_subject(subject):
    return re.sub(r"^(?:(?:re|fw|fwd|回复|答复|转发)\s*[:：]\s*)+", "", subject.strip(), flags=re.I)


def command(subject):
    match = re.match(r"^\[GPT(?::(NEW))?\]", base_subject(subject), re.I)
    return ("new" if match.group(1) else "chat") if match else None


def notice_recipients(config, data):
    """Read durable outgoing IDs, including notices from the pre-routing version."""
    recipients = dict(data.get("reply_recipients", {}))
    if config.notify_email:
        for event in data.get("events", []):
            if event.get("state") in {"sent", "uncertain"}:
                mid = event.get("reply_id", f"<mail-gpt-notice.{event['id']}@{config.email.split('@')[1]}>")
                recipients.setdefault(mid, event.get("recipient", config.notify_email))
    return recipients


def request_mode(mail, config):
    mode = command(mail.subject)
    if mode:
        return mode
    if base_subject(mail.subject) != "邮件机器人运行提醒":
        return None
    data = read_json(config.runtime / "notifications.json")
    recipients = notice_recipients(config, data)
    if any(recipients.get(mid) == mail.sender for mid in mail.in_reply_to + mail.references):
        return "chat"
    return None


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = tag in {"script", "style", "blockquote", "head"} or any(
            marker in attrs.get("class", "") for marker in ("gmail_quote", "yahoo_quoted"))
        if tag not in {"br", "hr", "img", "meta", "link", "input", "wbr"}:
            self.stack.append((tag, hidden))
            self.suppressed += hidden
        if not self.suppressed and tag in {"p", "div", "br", "li", "tr", "hr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.suppressed -= sum(hidden for _, hidden in self.stack[i:])
                del self.stack[i:]
                break
        if not self.suppressed and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def strip_quotes(text, is_reply):
    if not is_reply:
        return text.strip()
    lines = text.replace("\r\n", "\n").splitlines()
    result = []
    for i, line in enumerate(lines):
        # Common Gmail, Outlook and Chinese reply boundaries. Heuristic, not a full parser.
        joined = " ".join(lines[i:i + 3]).strip()
        if (re.match(r"^On .+wrote:\s*$", line.strip(), re.I)
                or (line.lstrip().startswith("On ") and re.match(r"^On .+wrote:", joined, re.I))
                or re.match(r"^在.+写道[：:]", line.strip())
                or re.match(r"^-{2,}\s*(Original(?: Message)?|原始邮件|原邮件)\s*-*\s*$", line.strip(), re.I)
                or re.match(r"^(From|发件人)\s*[:：]", line.strip(), re.I)):
            break
        if not line.lstrip().startswith(">"):
            result.append(line)
    return "\n".join(result).strip()


@dataclass
class Incoming:
    uid: str
    gmail_thread: str | None
    message_id: str
    sender: str
    recipients: list[str]
    subject: str
    in_reply_to: list[str]
    references: list[str]
    body: str
    attachments: list[dict]
    headers: EmailMessage


def parse_message(raw: bytes, uid="", gmail_thread=None):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    if len(msg.get_all("From", [])) != 1 or len(msg.get_all("Message-ID", [])) != 1:
        raise ValueError("Ambiguous identity headers")
    senders = getaddresses([str(msg["From"])])
    ids = message_ids(str(msg["Message-ID"]))
    if len(senders) != 1 or len(ids) != 1 or str(msg["Message-ID"]).strip() != ids[0]:
        raise ValueError("Invalid identity headers")
    irt = message_ids(str(msg.get("In-Reply-To", "")))
    refs = message_ids(str(msg.get("References", "")))
    attachments = []
    for part in msg.walk():
        if part.get_filename() or part.get_content_disposition() == "attachment":
            attachments.append({"filename": part.get_filename(), "content_type": part.get_content_type()})
    part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None and not part.get_filename() and part.get_content_disposition() != "attachment":
        try:
            body = part.get_content()
        except (LookupError, UnicodeError):
            body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            parser = HTMLText()
            parser.feed(body)
            body = "".join(parser.parts)
    return Incoming(str(uid), gmail_thread, ids[0], senders[0][1].lower(),
                    [v.lower() for _, v in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))],
                    str(msg.get("Subject", "")), irt, refs,
                    strip_quotes(body, bool(irt or refs)), attachments, msg)


def make_reply(mail: Incoming, sender: str, body: str, reply_id: str):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = sender
    # Reply-To and Cc from incoming messages never choose recipients.
    msg["To"] = mail.sender
    subject = base_subject(mail.subject)
    # NEW is a one-shot instruction; subsequent Reply subjects return to normal chat.
    subject = re.sub(r"^\[GPT:NEW\]", "[GPT]", subject, flags=re.I)
    if not command(subject):
        subject = "[GPT] " + subject
    msg["Subject"] = "Re: " + subject[:800]
    msg["Message-ID"] = reply_id
    msg["In-Reply-To"] = mail.message_id
    refs = list(dict.fromkeys(mail.references + mail.in_reply_to + [mail.message_id]))
    msg["References"] = " ".join(refs[-30:])
    msg["Date"] = formatdate(localtime=False)
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"
    msg["X-Mail-GPT"] = "1"
    msg.set_content(body)
    return msg.as_bytes()


class MailClient(Protocol):
    def candidates(self): ...
    def mark_seen(self, uid: str): ...


class SMTPNotSubmitted(RuntimeError):
    """Connection/authentication failed before any message was submitted."""


class SMTPClient:
    def __init__(self, config):
        self.config = config

    def send(self, recipient: str, raw: bytes):
        c = self.config
        context = ssl.create_default_context()
        client = None
        try:
            if c.smtp_security == "ssl":
                client = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=30, context=context)
            else:
                client = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=30)
            if c.smtp_security == "starttls":
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(c.email, c.password)
        except Exception as exc:
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass
            raise SMTPNotSubmitted("SMTP connection or login failed before submission") from exc
        try:
            refused = client.sendmail(c.email, [recipient], raw)
            if refused:
                raise RuntimeError("SMTP refused recipient")
        finally:
            # Once DATA was accepted, a failed QUIT must not turn success into a resend.
            client.close()


class IMAPClient:
    def __init__(self, config, since=None):
        self.config = config
        self.client = None
        self.since = since or config.imap_start_date or (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()

    def __enter__(self):
        c = self.config
        self.client = imaplib.IMAP4_SSL(c.imap_host, c.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
        try:
            self.client.login(c.email, c.password)
            self._ok(self.client.select(c.folder))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    @staticmethod
    def _ok(result):
        status, data = result
        if status != "OK":
            raise RuntimeError("IMAP operation failed")
        return data

    def candidates(self):
        client = self.client
        capabilities = {v.decode("ascii").upper() if isinstance(v, bytes) else v.upper() for v in client.capabilities}
        gmail = "X-GM-EXT-1" in capabilities
        since = date.fromisoformat(self.since)
        month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[since.month - 1]
        # Seen is a UI flag, never an acknowledgement that the bot handled a message.
        # Scan the allowlist across subjects. Routing decides what to answer, while
        # status can now expose legitimate mail excluded by subject rules.
        senders = sorted(self.config.allowed)
        if not senders:
            return
        terms = ['FROM "' + v.replace('\\', '\\\\').replace('"', '\\"') + '"' for v in senders]
        sender_query = terms[-1]
        for term in reversed(terms[:-1]):
            sender_query = "OR " + term + " " + sender_query
        query = f'SINCE {since.day:02d}-{month}-{since.year} ({sender_query})'
        uids = self._ok(client.uid("search", None, query))[0].split()
        for uid in sorted(uids, key=int):
            meta = self._ok(client.uid("fetch", uid, "(RFC822.SIZE)"))
            size = re.search(rb"RFC822.SIZE (\d+)", b" ".join(x for x in meta if isinstance(x, bytes)))
            if not size or int(size[1]) > self.config.max_message_bytes:
                continue
            fields = "(BODY.PEEK[] X-GM-THRID)" if gmail else "(BODY.PEEK[])"
            data = self._ok(client.uid("fetch", uid, fields))
            for item in data:
                if isinstance(item, tuple):
                    attrs, raw = item
                    if len(raw) > self.config.max_message_bytes:
                        continue
                    match = re.search(rb"X-GM-THRID (\d+)", attrs)
                    try:
                        yield parse_message(raw, uid.decode(), match[1].decode() if match else None)
                    except (ValueError, TypeError, LookupError):
                        continue

    # Compatibility with callers of the original MVP; now includes read messages too.
    unread = candidates

    def mark_seen(self, uid):
        self._ok(self.client.uid("store", uid, "+FLAGS.SILENT", "(\\Seen)"))

    def __exit__(self, *_):
        if self.client:
            try:
                self.client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

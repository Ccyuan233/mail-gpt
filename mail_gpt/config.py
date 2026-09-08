from dataclasses import dataclass, field
from pathlib import Path
from datetime import date
import os
import re


def read_env(path: Path) -> dict[str, str]:
    """Small literal .env reader: no interpolation, execution, or global env writes."""
    result = {}
    if path.exists():
        for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
                raise ValueError(f"Invalid .env line {n}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return result


def address(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
        raise ValueError("An email setting is not a plain email address")
    return value


@dataclass(frozen=True)
class Config:
    email: str
    password: str = field(repr=False)
    allowed: frozenset[str]
    database: Path
    codex_home: Path
    workspace: Path
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    folder: str = "INBOX"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_security: str = "ssl"
    poll_interval: int = 30
    max_message_bytes: int = 1048576
    max_prompt_chars: int = 24000
    max_requests_per_hour: int = 30
    require_dmarc: bool = True
    auth_serv_id: str = "mx.google.com"
    codex_path: str = "codex"
    codex_timeout: int = 480
    model: str = ""
    expected_version: str = "0.153.4"
    imap_start_date: str = ""
    notify_email: str = ""
    web_search: str = "live"

    @property
    def runtime(self):
        return self.database.with_suffix(".runtime")

    @classmethod
    def load(cls, path: Path, *, require_mail: bool = True):
        path = path.resolve()
        env = read_env(path)
        # Only configuration names from the file/defaults are consulted.
        def get(key, default=""):
            return os.environ.get(key, env.get(key, default))
        def num(key, default):
            n = int(get(key, str(default)))
            if n < 1:
                raise ValueError(f"{key} must be positive")
            return n
        def loc(key, default):
            return (path.parent / get(key, default)).resolve()
        email = get("EMAIL_ADDRESS")
        allowed = frozenset(address(v) for v in get("ALLOWED_SENDERS").split(",") if v.strip())
        password = get("EMAIL_PASSWORD")
        if email:
            email = address(email)
        if require_mail and (not email or not password or not allowed):
            raise ValueError("Configure EMAIL_ADDRESS, EMAIL_PASSWORD and ALLOWED_SENDERS in .env")
        if email and email in allowed:
            raise ValueError("The bot account must differ from every allowed sender")
        security = get("SMTP_SECURITY", "ssl")
        if security not in {"ssl", "starttls"}:
            raise ValueError("SMTP_SECURITY must be ssl or starttls")
        dmarc = get("REQUIRE_DMARC", "true").lower()
        if dmarc not in {"true", "false"}:
            raise ValueError("REQUIRE_DMARC must be true or false")
        start_date = get("IMAP_START_DATE")
        web_search = get("CODEX_WEB_SEARCH", "live").lower()
        if web_search not in {"disabled", "live"}:
            raise ValueError("CODEX_WEB_SEARCH must be disabled or live")
        if start_date:
            date.fromisoformat(start_date)
        notify = get("NOTIFY_EMAIL")
        if notify:
            notify = address(notify)
            if notify not in allowed or notify == email:
                raise ValueError("NOTIFY_EMAIL must be an allowed sender, not the bot account")
        return cls(email=email, password=password, allowed=allowed,
                   database=loc("DATABASE_PATH", "./data/conversations.db"),
                   codex_home=loc("BOT_CODEX_HOME", "./data/codex-home"),
                   workspace=loc("CODEX_WORKSPACE", "./data/empty-workspace"),
                   imap_host=get("IMAP_HOST", "imap.gmail.com"), imap_port=num("IMAP_PORT", 993),
                   folder=get("IMAP_FOLDER", "INBOX"), smtp_host=get("SMTP_HOST", "smtp.gmail.com"),
                   smtp_port=num("SMTP_PORT", 465), smtp_security=security,
                   poll_interval=num("POLL_INTERVAL", 30), max_message_bytes=num("MAX_MESSAGE_BYTES", 1048576),
                   max_prompt_chars=num("MAX_PROMPT_CHARS", 24000),
                   max_requests_per_hour=num("MAX_REQUESTS_PER_HOUR", 30),
                   require_dmarc=dmarc == "true", auth_serv_id=get("AUTH_SERV_ID", "mx.google.com"),
                   codex_path=get("CODEX_PATH", "codex"), codex_timeout=num("CODEX_TIMEOUT", 480),
                   model=get("CODEX_MODEL"), expected_version=get("CODEX_EXPECTED_VERSION", "0.153.4"),
                   imap_start_date=start_date, notify_email=notify, web_search=web_search)

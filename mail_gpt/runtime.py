"""Atomic operational state, separate from message delivery transactions."""
import json
import os
from pathlib import Path
import time
import uuid


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {} if default is None else default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Health:
    def __init__(self, config):
        self.path = config.runtime / "worker.json"
        self.data = {"pid": os.getpid(), "run_id": os.environ.get("MAIL_GPT_RUN_ID", uuid.uuid4().hex),
                     "started_at": time.time(), "last_success": None, "consecutive_failures": 0,
                     "web_search": config.web_search, "codex_timeout": config.codex_timeout}

    def beat(self, phase):
        self.data.update(phase=phase, updated_at=time.time())
        write_json(self.path, self.data)

    def success(self):
        self.data.update(last_success=time.time(), consecutive_failures=0, failure_category=None)
        self.beat("idle")

    def failure(self, exc):
        self.data["consecutive_failures"] += 1
        self.data["failure_category"] = type(exc).__name__
        self.beat("poll-failed")


def is_paused(config):
    return (config.runtime / "paused").exists()


def control(config, action):
    if action == "pause":
        write_json(config.runtime / "paused", {"requested_at": time.time()})
        return {"paused": True, "message": "Current request finishes before stopping; automatic restarts stay paused."}
    if action == "resume":
        (config.runtime / "paused").unlink(missing_ok=True)
        return {"paused": False, "message": "Start the scheduled task or supervisor to run immediately."}
    if action == "notify-test":
        if not config.notify_email:
            raise ValueError("Configure NOTIFY_EMAIL before testing notifications")
        write_json(config.runtime / "test-notification.json", {"id": uuid.uuid4().hex, "requested_at": time.time()})
        return {"queued": True, "recipient": config.notify_email}
    state = read_json(config.runtime / "worker.json")
    supervisor = read_json(config.runtime / "supervisor.json")
    notices = read_json(config.runtime / "notifications.json", {"events": []})
    return {"paused": is_paused(config), "notify_email": config.notify_email,
            "worker": state, "supervisor": supervisor,
            "heartbeat_age_seconds": round(time.time() - state["updated_at"]) if state else None,
            "notifications": [{k: e.get(k) for k in ("id", "kind", "state", "created_at", "last_attempt")}
                              for e in notices["events"][-10:]]}

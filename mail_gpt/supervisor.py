"""Restart crashed workers and report faults without retrying uncertain replies."""
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from .mail import SMTPClient
from .notifications import Notifications
from .runtime import is_paused, read_json, write_json
from .storage import instance_lock


class Supervisor:
    def __init__(self, config, env_path, notices, clock=time.time, spawn=subprocess.Popen):
        self.config, self.env_path, self.notices = config, Path(env_path).resolve(), notices
        self.clock, self.spawn = clock, spawn
        self.child = None
        self.run_id = None
        self.started = 0
        self.failures = 0
        self.next_start = 0
        self.pausing = False
        self.external = False
        self.created_at = clock()

    def start_worker(self):
        c = self.config
        # A surviving/manual worker is protected by its own lock. Wait for it
        # instead of repeatedly starting duplicate workers and reporting crashes.
        try:
            with instance_lock(c.database.with_suffix(".lock")):
                pass
        except OSError:
            self.external = True
            return False
        self.external = False
        self.run_id = uuid.uuid4().hex
        env = dict(os.environ, MAIL_GPT_SUPERVISED="1", MAIL_GPT_RUN_ID=self.run_id)
        self.child = self.spawn([sys.executable, "-m", "mail_gpt", "--env", str(self.env_path), "run"],
            cwd=self.env_path.parent, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.started = self.clock()
        logging.info("worker_launched pid=%s", self.child.pid)
        return True

    def request_faults(self):
        c = self.config
        if not c.database.exists():
            return
        db = sqlite3.connect(c.database.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = db.execute("SELECT message_id,state,error_id FROM messages WHERE account=? "
                              "AND (state='review' OR error_id IS NOT NULL) ORDER BY message_id", (c.email,)).fetchall()
        finally:
            db.close()
        if rows:
            fingerprint = hashlib.sha256(repr(rows).encode()).hexdigest()
            self.notices.issue("requests", fingerprint,
                f"有 {len(rows)} 封请求出现 AI 处理失败或需要人工核对。原请求不会自动重复生成或盲目重发。"
                "请查看本机 review/status 记录；可以发送 [GPT:NEW] 邮件开始独立新话题。")
        else:
            self.notices.clear("requests", "此前需要核对的请求已处理。")

    def tick(self):
        c, now = self.config, self.clock()
        self.pausing = is_paused(c)
        if self.child is not None and self.child.poll() is not None:
            code = self.child.returncode
            self.child = None
            if not self.pausing:
                self.failures += 1
                delay = min(30 * (2 ** min(self.failures - 1, 5)), 900)
                self.next_start = now + delay
                self.notices.issue("worker", "down", f"邮件机器人进程意外退出（退出码 {code}）。"
                    "守护程序会自动重启，连续失败时会逐步延长重试间隔，最长 15 分钟。"
                    "中断时投递结果不确定的邮件仍需人工核对。")
        if not self.pausing and self.child is None and now >= self.next_start:
            try:
                self.start_worker()
            except OSError as exc:
                self.next_start = now + 60
                self.notices.issue("worker", "down", "无法启动邮件机器人进程，守护程序将继续重试。错误类别：" + type(exc).__name__)
        health = read_json(c.runtime / "worker.json")
        ours = self.child is not None and health.get("run_id") == self.run_id
        # If a prior supervisor died, the worker may still own the single-instance
        # lock. Its fresh heartbeat can still be inspected without terminating it.
        current = ours or self.external
        if current and not self.pausing:
            if health.get("consecutive_failures", 0) >= 3:
                self.notices.issue("mailbox", "failing", "连续 3 轮收信或处理失败，程序仍在自动重试。错误类别："
                                   + str(health.get("failure_category", "unknown")))
            elif health.get("consecutive_failures", 0) == 0 and health.get("last_success"):
                self.notices.clear("mailbox", "邮箱轮询已经恢复正常。")
                self.notices.clear("worker", "邮件机器人已重新运行，并成功完成一轮邮箱检查。")
                if now - self.started > 60:
                    self.failures = 0
        if (self.child is not None or self.external) and not self.pausing:
            last = health.get("updated_at", self.created_at) if current else self.started
            if now - last > max(600, c.codex_timeout + 240):
                self.notices.issue("stalled", "stale", "机器人较长时间没有更新进度。为避免中断投递造成重复邮件，"
                    "未强制结束进程；需要检查本机运行情况。")
            else:
                self.notices.clear("stalled", "机器人进度已恢复更新。")
        if not self.pausing:
            try:
                self.request_faults()
            except sqlite3.Error as exc:
                self.notices.issue("storage", "unavailable", "暂时无法核对邮件处理记录，将继续尝试。错误类别：" + type(exc).__name__)
            else:
                self.notices.clear("storage", "邮件处理记录已经可以正常读取。")
            test_path = c.runtime / "test-notification.json"
            test = read_json(test_path)
            if test:
                self.notices.issue("test", test["id"], "这是一封故障提醒通道测试邮件，不代表机器人出现了故障。"
                                   "自动启动与守护程序已经启用。")
                test_path.unlink(missing_ok=True)
            self.notices.flush()
        write_json(c.runtime / "supervisor.json", {"pid": os.getpid(), "updated_at": now,
            "worker_pid": self.child.pid if self.child is not None else None,
            "paused": self.pausing, "restart_failures": self.failures, "next_start": self.next_start})
        return not (self.pausing and self.child is None)


def supervise(config, env_path):
    c = config
    c.runtime.mkdir(parents=True, exist_ok=True)
    if is_paused(c):
        return 0
    # Do not treat a duplicate scheduler trigger as a process fault.
    guard = instance_lock(c.runtime / "supervisor.lock")
    try:
        guard.__enter__()
    except OSError:
        return 0
    try:
        handler = RotatingFileHandler(c.runtime / "supervisor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[handler], force=True)
        runner = Supervisor(c, env_path, Notifications(c, SMTPClient(c)))
        while runner.tick():
            time.sleep(5)
        return 0
    finally:
        guard.__exit__(None, None, None)

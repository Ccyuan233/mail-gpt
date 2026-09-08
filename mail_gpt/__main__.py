import argparse
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import subprocess
import sys
import time
from .config import Config
from .codex import CodexRunner, BackendError
from .mail import IMAPClient, SMTPClient, request_mode
from .security import rejection
from .service import Service
from .storage import Store, instance_lock
from .runtime import Health, control, is_paused


def main():
    parser = argparse.ArgumentParser(description="Private email assistant using ChatGPT-authenticated Codex CLI")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Official interactive ChatGPT login in dedicated bot storage")
    doctor = commands.add_parser("doctor", help="Read-only preflight; never reads or sends mail")
    doctor.add_argument("--offline", action="store_true", help="Check CLI compatibility without requiring credentials")
    run = commands.add_parser("run", help="Process authorized mail and send replies")
    run.add_argument("--once", action="store_true")
    commands.add_parser("review", help="List uncertain/interrupted requests without message bodies")
    commands.add_parser("status", help="Read-only mailbox/database reconciliation, including read messages")
    resolve = commands.add_parser("resolve", help="Manually settle one uncertain request after checking Sent mail")
    resolve.add_argument("message_id")
    resolve.add_argument("action", choices=["sent", "skip", "retry-send"])
    commands.add_parser("smoke", help="Two real Codex turns; uses allowance, sends no email")
    commands.add_parser("supervise", help="Restart crashed workers and send coalesced fault notifications")
    for name in ("pause", "resume", "health", "notify-test"):
        commands.add_parser(name, help="Local supervisor control / notification channel test")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    health = None
    try:
        need_mail = args.command in {"run", "status", "supervise", "notify-test"} or (args.command == "doctor" and not args.offline)
        config = Config.load(args.env, require_mail=need_mail)
        if args.command in {"pause", "resume", "health", "notify-test"}:
            print(json.dumps(control(config, args.command), ensure_ascii=True, indent=2))
            return 0
        if args.command == "supervise":
            from .supervisor import supervise
            return supervise(config, args.env)
        if args.command == "run" and is_paused(config):
            print("Bot is paused. Run resume before starting it again.")
            return 0
        if args.command == "run" and os.environ.get("MAIL_GPT_SUPERVISED") == "1":
            config.runtime.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True,
                handlers=[RotatingFileHandler(config.runtime / "worker.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
        if args.command == "status":
            from .status import inspect_status
            print(json.dumps(inspect_status(config), ensure_ascii=True, indent=2))
            return 0
        if args.command in {"doctor", "login", "smoke"}:
            runner = CodexRunner(config)
            if args.command == "doctor":
                print(runner.check(require_login=not args.offline))
                print("Compatibility checks passed. " + ("Mail credentials and login not checked." if args.offline else "ChatGPT login confirmed. Mail connectivity not checked."))
            elif args.command == "login":
                runner.check(require_login=False)
                config.codex_home.mkdir(parents=True, exist_ok=True)
                # Run in the user's terminal so the official login URL/instructions remain visible.
                result = subprocess.run([runner.binary, "login"], env=runner.environment(), shell=False)
                return result.returncode
            else:
                runner.check()
                session = []
                marker = "mango-" + __import__("uuid").uuid4().hex[:12]
                runner.generate("[GPT] smoke", "Remember this exact test word: " + marker + ". Reply OK.", None, session.append)
                answer = runner.generate("[GPT] smoke", "What exact test word did I give you? Reply with only that word.", session[-1], session.append)
                if marker not in answer:
                    raise BackendError("Session continuity smoke test failed")
                print("Real CLI creation + resume passed; context was retained. No email sent.")
            return 0
        config.database.parent.mkdir(parents=True, exist_ok=True)
        with instance_lock(config.database.with_suffix(".lock")):
            db = Store(config.database)
            try:
                db.recover()
                if args.command == "review":
                    for row in db.review():
                        print(json.dumps(dict(row), ensure_ascii=True))
                    return 0
                if args.command == "resolve":
                    if not config.email:
                        raise ValueError("Configure EMAIL_ADDRESS first")
                    db.resolve_review(config.email, args.message_id, args.action)
                    print("Local state updated. retry-send is sent on the next run.")
                    return 0
                health = Health(config)
                health.beat("starting")
                backend = CodexRunner(config)
                backend.check()  # Fail before reading any mailbox when login/isolation is unavailable.
                service = Service(config, db, backend, SMTPClient(config), health)
                scan_start = db.scan_start(config.email, config.imap_start_date)
                logging.info("worker_started account=%s poll_interval=%s", config.email, config.poll_interval)
                while True:
                    if is_paused(config):
                        health.beat("stopped")
                        return 0
                    try:
                        health.beat("polling")
                        service.flush_outbox()
                        checked = 0
                        sent = 0
                        with IMAPClient(config, since=scan_start) as mailbox:
                            candidates = list(mailbox.candidates())
                            db.backfill_gmail_aliases(config.email, [m for m in candidates if not rejection(m, config) and request_mode(m, config)])
                            for mail in candidates:
                                if is_paused(config):
                                    break
                                checked += 1
                                state = service.process(mail)
                                if state in {"sent", "already-sent", "skipped"}:
                                    try:
                                        mailbox.mark_seen(mail.uid)
                                    except Exception as exc:
                                        # All candidates are already fetched. A cosmetic
                                        # flag failure must not starve later requests.
                                        logging.warning("mark_seen_failed category=%s", type(exc).__name__)
                                if state == "sent":
                                    sent += 1
                        logging.info("poll_complete checked=%s sent=%s", checked, sent)
                        health.success()
                    except Exception as exc:
                        # Never print raw network/server exceptions; they may contain credentials.
                        logging.error("poll_failed category=%s", type(exc).__name__)
                        health.failure(exc)
                        if args.once:
                            return 1
                    if args.once:
                        return 0
                    # Observe pause promptly while idle, without interrupting a turn.
                    for _ in range(config.poll_interval):
                        if is_paused(config):
                            break
                        time.sleep(1)
            finally:
                db.close()
    except KeyboardInterrupt:
        print("Stopped. Interrupted effects will require review at next startup.")
        return 130
    except (ValueError, BackendError) as exc:
        if health:
            health.failure(exc)
            logging.error("startup_failed category=%s", type(exc).__name__)
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        if health:
            health.failure(exc)
            logging.error("startup_failed category=%s", type(exc).__name__)
        print("Startup failed: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

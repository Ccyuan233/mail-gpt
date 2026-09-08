from dataclasses import replace
from email import policy
from email.parser import BytesParser
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from tests import test_bot as fixtures
from mail_gpt.__main__ import main
from mail_gpt.config import Config
from mail_gpt.mail import SMTPClient, SMTPNotSubmitted
from mail_gpt.notifications import Notifications
from mail_gpt.runtime import Health, control, is_paused, read_json, write_json
from mail_gpt.storage import instance_lock
from mail_gpt.supervisor import Supervisor


class SupervisorTests(unittest.TestCase):
    setUp = fixtures.BotTests.setUp
    tearDown = fixtures.BotTests.tearDown

    def notices(self):
        self.now = 1000
        self.config = replace(self.config, notify_email="me@sender.test")
        return Notifications(self.config, self.smtp, clock=lambda: self.now)

    def supervisor(self, notices):
        self.process = Mock(pid=123, returncode=None)
        self.process.poll.side_effect = lambda: self.process.returncode
        self.spawn = Mock(return_value=self.process)
        return Supervisor(self.config, self.config.database.parent / ".env", notices,
                          clock=lambda: self.now, spawn=self.spawn)

    def test_fault_notice_is_coalesced_and_durable(self):
        n = self.notices()
        n.issue("worker", "down", "Process stopped")
        n.flush()
        n = Notifications(self.config, self.smtp, clock=lambda: self.now)
        n.issue("worker", "down", "Still stopped")
        self.now += 3600
        n.flush()
        self.assertEqual(len(self.smtp.sent), 1)
        mail = BytesParser(policy=policy.default).parsebytes(self.smtp.sent[0][1])
        self.assertEqual(str(mail["To"]), self.config.notify_email)
        self.assertEqual(str(mail["Auto-Submitted"]), "auto-generated")
        self.assertFalse(str(mail["Subject"]).startswith("[GPT]"))
        self.assertNotIn("secret", mail.get_content())

    def test_known_connection_failure_is_queued_until_network_recovers(self):
        n = self.notices()
        n.issue("mailbox", "failing", "Connection failed")
        with patch.object(self.smtp, "send", side_effect=SMTPNotSubmitted("SECRET")):
            n.flush()
        self.assertEqual(n.data["events"][0]["state"], "pending")
        n.clear("mailbox", "Connection recovered")
        self.now += 301
        n.flush()
        self.assertEqual(len(self.smtp.sent), 1)
        body = BytesParser(policy=policy.default).parsebytes(self.smtp.sent[0][1]).get_content()
        self.assertIn("Connection recovered", body)
        self.assertNotIn("SECRET", body)

    def test_ambiguous_notification_delivery_is_never_blind_retried(self):
        n = self.notices()
        n.issue("worker", "down", "Failure")
        self.smtp.failure = True
        n.flush()
        self.now += 3600
        n = Notifications(self.config, self.smtp, clock=lambda: self.now)
        n.flush()
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(n.data["events"][0]["state"], "uncertain")

    def test_interrupted_notice_send_is_marked_uncertain_on_restart(self):
        n = self.notices()
        n.issue("worker", "down", "Failure")
        n.data["events"][0]["state"] = "sending"
        n.save()
        n = Notifications(self.config, self.smtp, clock=lambda: self.now)
        n.flush()
        self.assertEqual(n.data["events"][0]["state"], "uncertain")
        self.assertFalse(self.smtp.sent)

    def test_notification_cooldown_batches_new_faults(self):
        n = self.notices()
        n.issue("worker", "down", "Crash")
        n.flush()
        n.issue("mailbox", "failing", "Mail failure")
        n.issue("requests", "one", "Review required")
        n.flush()
        self.assertEqual(len(self.smtp.sent), 1)
        self.now += 301
        n.flush()
        self.assertEqual(len(self.smtp.sent), 2)
        body = BytesParser(policy=policy.default).parsebytes(self.smtp.sent[-1][1]).get_content()
        self.assertIn("Mail failure", body)
        self.assertIn("Review required", body)

    def test_supervisor_restarts_real_exited_process_after_backoff(self):
        n = self.notices()
        s = self.supervisor(n)
        process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"],
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        process.wait(timeout=10)
        s.child = process
        s.tick()
        self.assertEqual(s.next_start, 1030)
        self.spawn.assert_not_called()
        self.now += 30
        s.tick()
        self.spawn.assert_called_once()
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertIn("worker", n.data["active"])

    def test_supervisor_waits_for_existing_worker_lock(self):
        n = self.notices()
        s = self.supervisor(n)
        with instance_lock(self.config.database.with_suffix(".lock")):
            s.tick()
            self.spawn.assert_not_called()
            self.assertNotIn("worker", n.data["active"])
        s.tick()
        self.spawn.assert_called_once()

    def test_pause_survives_restart_and_never_kills_active_turn(self):
        n = self.notices()
        s = self.supervisor(n)
        s.tick()
        control(self.config, "pause")
        self.assertTrue(is_paused(self.config))
        self.assertTrue(s.tick(), "Wait for current child to finish")
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()
        self.process.returncode = 0
        self.assertFalse(s.tick())
        replacement = self.supervisor(n)
        self.assertFalse(replacement.tick())
        self.spawn.assert_not_called()
        self.assertNotIn("worker", n.data["active"])

    def test_worker_observes_pause_before_next_email(self):
        first = fixtures.incoming()
        second = fixtures.incoming("<later@sender.test>", thread="other")
        mailbox = Mock()
        mailbox.__enter__ = Mock(return_value=mailbox)
        mailbox.__exit__ = Mock(return_value=False)
        mailbox.candidates.return_value = [first, second]
        generate = self.backend.generate
        def pause_during_turn(*args):
            control(self.config, "pause")
            return generate(*args)
        with patch.object(self.backend, "generate", side_effect=pause_during_turn), \
                patch.object(self.backend, "check", create=True), \
                patch("sys.argv", ["mail_gpt", "run", "--once"]), \
                patch("mail_gpt.__main__.Config.load", return_value=self.config), \
                patch("mail_gpt.__main__.CodexRunner", return_value=self.backend), \
                patch("mail_gpt.__main__.SMTPClient", return_value=self.smtp), \
                patch("mail_gpt.__main__.IMAPClient", return_value=mailbox), \
                patch("mail_gpt.__main__.logging.basicConfig"):
            self.assertEqual(main(), 0)
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(self.db.message(self.config.email, first.message_id)["state"], "sent")
        self.assertIsNone(self.db.message(self.config.email, second.message_id))

    def test_mailbox_failure_threshold_and_recovery(self):
        n = self.notices()
        s = self.supervisor(n)
        s.tick()
        health = {"run_id": s.run_id, "updated_at": self.now, "consecutive_failures": 2,
                  "failure_category": "OSError", "last_success": None}
        write_json(self.config.runtime / "worker.json", health)
        s.tick()
        self.assertFalse(self.smtp.sent)
        health["consecutive_failures"] = 3
        write_json(self.config.runtime / "worker.json", health)
        s.tick()
        self.assertEqual(len(self.smtp.sent), 1)
        health.update(consecutive_failures=0, last_success=self.now)
        write_json(self.config.runtime / "worker.json", health)
        s.tick()
        self.assertNotIn("mailbox", n.data["active"])

    def test_stalled_worker_alert_does_not_kill_or_duplicate(self):
        self.config = replace(self.config, codex_timeout=480)
        n = self.notices()
        s = self.supervisor(n)
        s.tick()
        self.now += 601
        s.tick()
        self.assertNotIn("stalled", n.data["active"])
        self.now += 120
        s.tick()
        self.assertIn("stalled", n.data["active"])
        self.process.kill.assert_not_called()
        self.spawn.assert_called_once()

    def test_review_alert_does_not_retry_original_reply(self):
        n = self.notices()
        s = self.supervisor(n)
        row, _ = self.db.claim(self.config.email, fixtures.incoming())
        self.db.state(self.config.email, row["message_id"], "review", "interrupted")
        s.tick()
        self.assertIn("requests", n.data["active"])
        self.assertEqual(self.db.message(self.config.email, row["message_id"])["state"], "review")
        self.assertFalse(self.backend.calls)
        self.assertEqual(len(self.smtp.sent), 1, "Only a notice, never the original reply")

    def test_smtp_connection_failure_is_distinct_from_submission_failure(self):
        with patch("mail_gpt.mail.smtplib.SMTP_SSL", side_effect=OSError("SECRET")):
            with self.assertRaises(SMTPNotSubmitted):
                SMTPClient(self.config).send("me@sender.test", b"raw")
        client = Mock()
        client.sendmail.side_effect = OSError("Ambiguous disconnect")
        with patch("mail_gpt.mail.smtplib.SMTP_SSL", return_value=client):
            with self.assertRaises(OSError):
                SMTPClient(self.config).send("me@sender.test", b"raw")

    def test_notify_recipient_must_be_allowlisted(self):
        path = self.config.database.parent / ".env"
        path.write_text("EMAIL_ADDRESS=bot@receiver.test\nEMAIL_PASSWORD=secret\nALLOWED_SENDERS=me@sender.test\nNOTIFY_EMAIL=other@sender.test\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            Config.load(path)

    def test_health_does_not_store_exception_text(self):
        health = Health(self.config)
        health.failure(OSError("SECRET PASSWORD"))
        report = read_json(health.path)
        self.assertEqual(report["consecutive_failures"], 1)
        self.assertNotIn("SECRET", json.dumps(report))
        health.success()
        self.assertEqual(read_json(health.path)["consecutive_failures"], 0)

    def test_startup_failure_updates_health_without_raw_exception(self):
        from mail_gpt.codex import BackendError
        with patch("sys.argv", ["mail_gpt", "run", "--once"]), \
                patch("mail_gpt.__main__.Config.load", return_value=self.config), \
                patch("mail_gpt.__main__.CodexRunner", side_effect=BackendError("Missing executable")), \
                patch("mail_gpt.__main__.logging.basicConfig"), patch("builtins.print"):
            self.assertEqual(main(), 2)
        health = read_json(self.config.runtime / "worker.json")
        self.assertEqual(health["failure_category"], "BackendError")
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertNotIn("Missing executable", json.dumps(health))

    def test_pause_also_stops_remaining_saved_outbox_replies(self):
        from mail_gpt.mail import make_reply
        for mid in ("<first@sender.test>", "<second@sender.test>"):
            mail = fixtures.incoming(mid, thread=mid)
            row, _ = self.db.claim(self.config.email, mail)
            self.db.ready(self.config.email, mid, make_reply(mail, self.config.email, "Saved", row["reply_id"]))
        send = self.smtp.send
        def pause_after_send(*args):
            send(*args)
            control(self.config, "pause")
        self.service.health = Health(self.config)
        with patch.object(self.smtp, "send", side_effect=pause_after_send):
            self.service.flush_outbox()
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(len(self.db.ready_messages(self.config.email)), 1)


if __name__ == "__main__":
    unittest.main()

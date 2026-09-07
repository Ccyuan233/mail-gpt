"""Regression cases found during the reliability audit; no real mail is sent."""
import unittest
from unittest.mock import MagicMock, patch

from tests import test_bot as fixtures
from mail_gpt.__main__ import main
from mail_gpt.status import inspect_status


class AuditTests(unittest.TestCase):
    setUp = fixtures.BotTests.setUp
    tearDown = fixtures.BotTests.tearDown

    def test_new_thread_escapes_uncertain_old_delivery(self):
        self.smtp.failure = True
        old = fixtures.incoming()
        self.assertEqual(self.service.process(old), "review")
        self.smtp.failure = False
        fresh = fixtures.incoming("<new@sender.test>", subject="[GPT:NEW] Fresh")
        self.assertEqual(self.service.process(fresh), "sent")
        self.assertIsNone(self.backend.calls[-1][2])
        self.assertEqual(self.db.message(self.config.email, old.message_id)["state"], "review")
        self.assertEqual(len(self.smtp.sent), 2, "Do not resend the uncertain old answer")

    def test_new_thread_preserves_old_session_and_explicit_reply_branches(self):
        old = fixtures.incoming()
        self.service.process(old)
        old_row = self.db.message(self.config.email, old.message_id)
        old_session = self.db.conversation(old_row["thread_key"])["session_id"]
        fresh = fixtures.incoming("<new@sender.test>", subject="[GPT:NEW] Fresh")
        self.service.process(fresh)
        new_row = self.db.message(self.config.email, fresh.message_id)
        self.assertNotEqual(old_row["thread_key"], new_row["thread_key"])
        self.assertEqual(self.db.conversation(old_row["thread_key"])["session_id"], old_session)
        new_session = self.db.conversation(new_row["thread_key"])["session_id"]
        self.service.process(fixtures.incoming("<old-follow@sender.test>", refs=[old_row["reply_id"]]))
        self.assertEqual(self.backend.calls[-1][2], old_session)
        self.service.process(fixtures.incoming("<new-follow@sender.test>", refs=[new_row["reply_id"]]))
        self.assertEqual(self.backend.calls[-1][2], new_session)
        self.service.process(fixtures.incoming("<no-ref@sender.test>"))
        self.assertEqual(self.backend.calls[-1][2], new_session)

    def test_new_after_interrupted_generation_survives_restart_without_duplication(self):
        old = fixtures.incoming()
        old_row, _ = self.db.claim(self.config.email, old)
        self.db.state(self.config.email, old.message_id, "generating")
        self.db.recover()
        fresh = fixtures.incoming("<recovery@sender.test>", subject="[GPT:NEW] Recovery")
        self.assertEqual(self.service.process(fresh), "sent")
        row = self.db.message(self.config.email, fresh.message_id)
        session = self.db.conversation(row["thread_key"])["session_id"]
        self.db.close()
        self.db = fixtures.Store(self.config.database)
        self.service.store = self.db
        self.db.recover()
        self.assertEqual(self.service.process(fresh), "already-sent")
        self.service.process(fixtures.incoming("<continue@sender.test>", refs=[row["reply_id"]]))
        self.assertEqual(self.backend.calls[-1][2], session)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertTrue(self.db.conversation(old_row["thread_key"])["blocked"])
        self.assertEqual(self.db.message(self.config.email, old.message_id)["state"], "review")

    def test_seen_flag_failure_does_not_starve_later_requests(self):
        first = fixtures.incoming()
        self.service.process(first)
        second = fixtures.incoming("<later@sender.test>", thread="other")
        mailbox = MagicMock()
        mailbox.__enter__.return_value = mailbox
        mailbox.candidates.return_value = [first, second]
        mailbox.mark_seen.side_effect = OSError("Mailbox disconnected while marking Seen")
        with patch("sys.argv", ["mail_gpt", "run", "--once"]), \
                patch("mail_gpt.__main__.Config.load", return_value=self.config), \
                patch("mail_gpt.__main__.CodexRunner", return_value=self.backend), \
                patch.object(self.backend, "check", create=True), \
                patch("mail_gpt.__main__.SMTPClient", return_value=self.smtp), \
                patch("mail_gpt.__main__.IMAPClient", return_value=mailbox), \
                patch("mail_gpt.__main__.logging.basicConfig"):
            result = main()
        self.assertEqual(result, 0)
        self.assertEqual(self.db.message(self.config.email, second.message_id)["state"], "sent")
        self.assertEqual(len(self.smtp.sent), 2)

    def test_status_uses_records_after_mailbox_scan(self):
        mail = fixtures.incoming()
        mailbox = MagicMock()
        mailbox.__enter__.return_value = mailbox
        def arriving_during_scan():
            self.service.process(mail)
            yield mail
        mailbox.candidates.side_effect = arriving_during_scan
        report = inspect_status(self.config, mailbox_factory=lambda *a, **kw: mailbox)
        self.assertEqual(report["mailbox_counts"], {"sent": 1})
        self.assertEqual(report["database_counts"], {"sent": 1})
        mailbox.mark_seen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

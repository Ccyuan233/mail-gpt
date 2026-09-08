import unittest
from unittest.mock import Mock, MagicMock
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from tests import test_bot as fixtures
from mail_gpt.mail import IMAPClient, make_reply, request_mode
from mail_gpt.notifications import Notifications
from mail_gpt.runtime import write_json
from mail_gpt.status import inspect_status


class NoticeReplyTests(unittest.TestCase):
    setUp = fixtures.BotTests.setUp
    tearDown = fixtures.BotTests.tearDown

    def notice_reply(self):
        self.config = replace(self.config, notify_email="me@sender.test")
        self.service.config = self.config
        write_json(self.config.runtime / "notifications.json", {
            "events": [{"id": "test-notice", "state": "sent"}]})
        return fixtures.incoming("<notice-follow@sender.test>", thread="notice-thread",
            subject="回复：邮件机器人运行提醒", refs=["<mail-gpt-notice.test-notice@receiver.test>"],
            body="新问题\n\n---原始邮件---\n发件人: bot\n旧通知")

    def test_notice_reply_reaches_imap_scan_without_gpt_subject(self):
        mail = self.notice_reply()
        fake = Mock(capabilities=("IMAP4REV1",))
        def uid(*args):
            if args[0] == "search":
                return "OK", [b"" if 'SUBJECT "[GPT"' in str(args) else b"1"]
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"1 (RFC822.SIZE 900)"]
            return "OK", [(b"1 (BODY[])", mail.headers.as_bytes()), b")"]
        fake.uid.side_effect = uid
        box = IMAPClient(self.config)
        box.client = fake
        mails = list(box.candidates())
        self.assertEqual(len(mails), 1, "Replies to notices must reach routing")

    def test_authenticated_reply_to_actual_notice_gets_one_answer(self):
        mail = self.notice_reply()
        self.assertEqual(self.service.process(mail), "sent")
        self.assertEqual(self.service.process(mail), "already-sent")
        self.assertEqual(self.backend.calls[0][1], "新问题")
        self.assertEqual(len(self.smtp.sent), 1)

    def test_ordinary_mail_is_visible_as_filtered_in_status(self):
        mail = fixtures.incoming(subject="普通邮件")
        box = MagicMock()
        box.__enter__.return_value = box
        box.candidates.return_value = [mail]
        report = inspect_status(self.config, mailbox_factory=lambda *a, **kw: box)
        self.assertEqual(report["mailbox_counts"], {"filtered": 1})
        self.assertEqual(report["messages"][0]["filter_reason"], "subject")

    def test_unknown_notice_reference_or_auto_reply_is_not_allowed(self):
        mail = self.notice_reply()
        mail.in_reply_to = mail.references = ["<mail-gpt-notice.fake@receiver.test>"]
        self.assertEqual(self.service.process(mail), "ignored")
        mail = self.notice_reply()
        mail.headers["Auto-Submitted"] = "auto-replied"
        self.assertEqual(self.service.process(mail), "ignored")
        self.assertFalse(self.backend.calls)

    def test_notice_answer_moves_subject_to_normal_chat(self):
        mail = self.notice_reply()
        reply = BytesParser(policy=policy.default).parsebytes(make_reply(mail, self.config.email, "Answer", "<reply@receiver.test>"))
        self.assertTrue(str(reply["Subject"]).startswith("Re: [GPT]"))

    def test_status_counts_notice_reply_as_pending(self):
        mail = self.notice_reply()
        box = MagicMock()
        box.__enter__.return_value = box
        box.candidates.return_value = [mail]
        report = inspect_status(self.config, mailbox_factory=lambda *a, **kw: box)
        self.assertEqual(report["mailbox_counts"], {"pending-not-recorded": 1})
        self.assertIsNone(report["messages"][0]["filter_reason"])

    def test_notice_id_survives_event_history_pruning(self):
        config = replace(self.config, notify_email="me@sender.test")
        notices = Notifications(config, self.smtp)
        notices.issue("test", "first", "Test notification")
        notices.flush()
        sent = BytesParser(policy=policy.default).parsebytes(self.smtp.sent[0][1])
        notices.data["events"] = []
        notices.save()
        mail = fixtures.incoming(subject="回复：邮件机器人运行提醒", refs=[str(sent["Message-ID"])])
        self.assertEqual(request_mode(mail, config), "chat")

    def test_notice_recipient_mapping_does_not_cross_senders(self):
        config = replace(self.config, notify_email="me@sender.test",
                         allowed=frozenset({"me@sender.test", "other@sender.test"}))
        mid = "<mail-gpt-notice.actual@receiver.test>"
        write_json(config.runtime / "notifications.json", {"reply_recipients": {mid: "me@sender.test"}})
        other = fixtures.incoming(subject="回复：邮件机器人运行提醒", sender="other@sender.test", refs=[mid])
        self.assertIsNone(request_mode(other, config))


if __name__ == "__main__":
    unittest.main()

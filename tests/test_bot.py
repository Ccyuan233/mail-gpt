from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from mail_gpt.config import Config, read_env
from mail_gpt.codex import CodexRunner, BackendError, REQUIRED_FEATURES
from mail_gpt.mail import parse_message, make_reply, command, IMAPClient, SMTPClient
from mail_gpt.security import rejection
from mail_gpt.storage import Store, instance_lock
from mail_gpt.service import Service


def incoming(mid="<one@sender.test>", thread="12345678901234567890", subject="[GPT] Physics", sender="me@sender.test", refs=None, body="Why?"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "bot@receiver.test"
    msg["Subject"] = subject
    msg["Message-ID"] = mid
    msg["Authentication-Results"] = "mx.google.com; dmarc=pass header.from=sender.test"
    if refs:
        msg["In-Reply-To"] = refs[-1]
        msg["References"] = " ".join(refs)
    msg.set_content(body)
    return parse_message(msg.as_bytes(), "1", thread)


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.failure = False

    def generate(self, subject, body, session, on_session):
        self.calls.append((subject, body, session))
        on_session(session or str(uuid.uuid4()))
        if self.failure:
            raise RuntimeError("SECRET /private/token password")
        return "The answer is 42."


class FakeSMTP:
    def __init__(self):
        self.sent = []
        self.failure = False

    def send(self, recipient, raw):
        self.sent.append((recipient, raw))
        if self.failure:
            raise OSError("Ambiguous disconnect after DATA")


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        p = Path(self.temp.name)
        self.config = Config("bot@receiver.test", "secret", frozenset({"me@sender.test"}), p / "db.sqlite", p / "codex", p / "empty")
        self.db = Store(self.config.database)
        self.backend = FakeBackend()
        self.smtp = FakeSMTP()
        self.service = Service(self.config, self.db, self.backend, self.smtp)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_same_gmail_thread_resumes_persistent_session(self):
        self.service.process(incoming())
        first = self.db.message(self.config.email, "<one@sender.test>")
        sid = self.db.conversation(first["thread_key"])["session_id"]
        self.db.close()
        self.db = Store(self.config.database)
        self.service.store = self.db
        self.service.process(incoming("<two@sender.test>"))
        self.assertEqual(self.backend.calls[-1][2], sid)

    def test_duplicate_message_id_does_not_generate_or_send_twice(self):
        mail = incoming()
        self.service.process(mail)
        self.service.process(mail)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.smtp.sent), 1)

    def test_rfc_fallback_maps_reply_to_outgoing_id(self):
        first = incoming(thread=None)
        self.service.process(first)
        row = self.db.message(self.config.email, first.message_id)
        follow = incoming("<two@sender.test>", thread=None, refs=[row["reply_id"]])
        self.service.process(follow)
        self.assertEqual(self.db.message(self.config.email, follow.message_id)["thread_key"], row["thread_key"])
        self.assertIsNotNone(self.backend.calls[-1][2])

    def test_distinct_threads_get_distinct_sessions(self):
        self.service.process(incoming())
        self.service.process(incoming("<two@sender.test>", thread="different"))
        self.assertIsNone(self.backend.calls[-1][2])

    def test_sender_scoping_prevents_cross_user_context_leak(self):
        self.service.process(incoming())
        other = incoming("<other@sender.test>", sender="other@sender.test")
        self.assertNotEqual(self.db.resolve(self.config.email, other), self.db.message(self.config.email, "<one@sender.test>")["thread_key"])

    def test_new_resets_once_and_reply_subject_returns_to_chat(self):
        self.service.process(incoming())
        self.service.process(incoming("<new@sender.test>", subject="Re: [GPT:NEW] Physics"))
        self.assertIsNone(self.backend.calls[-1][2])
        reply = BytesParser(policy=policy.default).parsebytes(self.smtp.sent[-1][1])
        self.assertEqual(command(str(reply["Subject"])), "chat")
        self.service.process(incoming("<three@sender.test>", thread="new-gmail-group", subject=str(reply["Subject"]), refs=[str(reply["Message-ID"])]))
        self.assertIsNotNone(self.backend.calls[-1][2])

    def test_header_threading_and_reply_to_cannot_redirect(self):
        mail = incoming(refs=["<root@sender.test>"])
        mail.headers["Reply-To"] = "attacker@evil.test"
        raw = make_reply(mail, self.config.email, "answer", "<reply@receiver.test>")
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(msg["To"], mail.sender)
        self.assertEqual(msg["In-Reply-To"], mail.message_id)
        self.assertIn("<root@sender.test>", msg["References"])
        self.assertEqual(msg["Auto-Submitted"], "auto-replied")
        self.assertNotIn("Cc", msg)

    def test_reject_self_and_unknown_sender_and_wrong_recipient(self):
        for sender in ["bot@receiver.test", "evil@evil.test"]:
            self.assertEqual(self.service.process(incoming(sender=sender)), "ignored")
        mail = incoming()
        mail.recipients = ["someone@else.test"]
        self.assertEqual(self.service.process(mail), "ignored")
        self.assertFalse(self.backend.calls)

    def test_auto_reply_loop_headers(self):
        for key, value in [("Auto-Submitted", "auto-replied"), ("Precedence", "bulk"),
                           ("Precedence", "list"), ("X-Auto-Response-Suppress", "All"),
                           ("X-Mail-GPT", "1"), ("List-Id", "list"), ("Return-Path", "<>")]:
            with self.subTest(key=key, value=value):
                mail = incoming()
                mail.headers[key] = value
                self.assertIsNotNone(rejection(mail, self.config))

    def test_duplicate_auto_headers_cannot_hide_automation(self):
        mail = incoming()
        mail.headers["Auto-Submitted"] = "no"
        mail.headers["Auto-Submitted"] = "auto-generated"
        self.assertEqual(rejection(mail, self.config), "auto-submitted")

    def test_dmarc_checks_topmost_result_and_alignment(self):
        for result in ["attacker.test; dmarc=pass header.from=sender.test",
                       "mx.google.com; dmarc=fail header.from=sender.test",
                       "mx.google.com; dmarc=pass header.from=evil.test"]:
            mail = incoming()
            mail.headers.replace_header("Authentication-Results", result)
            mail.headers["Authentication-Results"] = "mx.google.com; dmarc=pass header.from=sender.test"
            self.assertEqual(rejection(mail, self.config), "authentication")

    def test_subject_commands_and_unsupported_modes(self):
        for subj in ["[CODEX] delete", "hello [GPT]", "[GPT:SEARCH] hi", "[GPTX]spoof"]:
            self.assertEqual(self.service.process(incoming(subject=subj)), "ignored")
        self.assertEqual(command("Re: 回复: [GPT] hello"), "chat")

    def test_quote_stripping_does_not_duplicate_history(self):
        mail = incoming(refs=["<old@sender.test>"], body="Now what?\n\nOn Monday Bob wrote:\n> Old question\n> old answer")
        self.assertEqual(mail.body, "Now what?")
        original = incoming(body="> This quote is my actual question")
        self.assertIn("> This quote", original.body)

    def test_html_and_attachment_are_not_executed_or_used_as_prompt(self):
        msg = incoming().headers
        msg.clear_content()
        msg.set_content("<div>New question</div><blockquote>Old answer</blockquote><script>bad()</script>", subtype="html")
        msg.add_attachment(b"delete everything", maintype="application", subtype="octet-stream", filename="evil.ps1")
        mail = parse_message(msg.as_bytes())
        self.assertEqual(mail.body, "New question")
        self.assertEqual(mail.attachments[0]["filename"], "evil.ps1")

    def test_plaintext_preferred_over_html(self):
        msg = incoming().headers
        msg.add_alternative("<p>Other HTML</p>", subtype="html")
        self.assertEqual(parse_message(msg.as_bytes()).body, "Why?")

    def test_missing_and_duplicate_identity_rejected(self):
        msg = incoming().headers
        del msg["Message-ID"]
        with self.assertRaises(ValueError):
            parse_message(msg.as_bytes())
        raw = b"From: me@sender.test\r\nFrom: evil@evil.test\r\nMessage-ID: <a@x.test>\r\n\r\nbody"
        with self.assertRaises(ValueError):
            parse_message(raw)

    def test_failed_generation_sends_sanitized_error_once_blocks_resume(self):
        self.backend.failure = True
        self.service.process(incoming())
        reply = self.smtp.sent[-1][1]
        self.assertIn(b"Error ID:", reply)
        self.assertNotIn(b"SECRET", reply)
        self.service.process(incoming())
        self.service.process(incoming("<two@sender.test>"))
        self.assertEqual(len(self.backend.calls), 1)
        self.backend.failure = False
        self.service.process(incoming("<new@sender.test>", subject="[GPT:NEW] recovery"))
        self.assertEqual(len(self.backend.calls), 2)

    def test_uncertain_send_is_never_automatically_retried(self):
        self.smtp.failure = True
        mail = incoming()
        self.assertEqual(self.service.process(mail), "review")
        self.db.recover()
        self.service.flush_outbox()
        self.service.process(mail)
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.service.process(incoming("<two@sender.test>")), "waiting")
        self.db.resolve_review(self.config.email, mail.message_id, "retry-send")
        self.smtp.failure = False
        self.service.flush_outbox()
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(self.smtp.sent[0], self.smtp.sent[1])

    def test_ready_outbox_survives_restart_without_regeneration(self):
        mail = incoming()
        row, _ = self.db.claim(self.config.email, mail)
        self.db.ready(self.config.email, mail.message_id, make_reply(mail, self.config.email, "cached", row["reply_id"]))
        self.db.recover()
        self.service.flush_outbox()
        self.assertFalse(self.backend.calls)
        self.assertEqual(len(self.smtp.sent), 1)

    def test_outbox_respects_revoked_sender_permission(self):
        mail = incoming()
        row, _ = self.db.claim(self.config.email, mail)
        self.db.ready(self.config.email, mail.message_id, make_reply(mail, self.config.email, "cached", row["reply_id"]))
        self.service.config = replace(self.config, allowed=frozenset())
        self.service.flush_outbox()
        self.assertFalse(self.smtp.sent)
        self.assertEqual(self.db.message(self.config.email, mail.message_id)["state"], "review")

    def test_interrupted_generation_requires_review_and_blocks_history(self):
        mail = incoming()
        row, _ = self.db.claim(self.config.email, mail)
        self.db.state(self.config.email, mail.message_id, "generating")
        self.db.recover()
        self.assertEqual(self.db.message(self.config.email, mail.message_id)["state"], "review")
        self.assertTrue(self.db.conversation(row["thread_key"])["blocked"])
        with self.assertRaises(ValueError):
            self.db.resolve_review(self.config.email, mail.message_id, "retry-send")

    def test_rate_limit_and_oversized_input_do_not_call_backend(self):
        self.service.config = replace(self.config, max_requests_per_hour=1)
        self.service.process(incoming(body="x" * 25000))
        self.assertFalse(self.backend.calls)
        self.assertEqual(self.service.process(incoming("<two@sender.test>")), "rate-limited")

    def test_process_lock_excludes_second_instance(self):
        path = self.config.database.with_suffix(".lock")
        with instance_lock(path):
            with self.assertRaises(OSError):
                with instance_lock(path):
                    self.fail("Second instance acquired lock")

    def test_environment_does_not_expose_mail_or_api_secrets(self):
        runner = object.__new__(CodexRunner)
        runner.config = self.config
        with patch.dict(os.environ, {"EMAIL_PASSWORD": "mail-secret", "OPENAI_API_KEY": "api-secret", "CODEX_THREAD_ID": "parent"}):
            env = runner.environment()
        self.assertNotIn("EMAIL_PASSWORD", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertEqual(env["CODEX_HOME"], str(self.config.codex_home))

    def test_adapter_validates_json_and_same_resume_id(self):
        sid = str(uuid.uuid4())
        events = json.dumps({"type": "thread.started", "thread_id": sid}) + '\n{"type":"turn.completed"}'
        seen = []
        self.assertEqual(CodexRunner.parse_events(events, seen.append, sid), sid)
        self.assertEqual(seen, [sid])
        for text in ["invalid json", '{"type":"turn.completed"}', events.replace(sid, str(uuid.uuid4()))]:
            with self.assertRaises((BackendError, ValueError)):
                CodexRunner.parse_events(text, seen.append, sid)

    def test_tool_and_failure_events_are_rejected(self):
        for event in [{"type": "turn.failed"}, {"type": "error"},
                      {"type": "item.completed", "item": {"type": "command_execution"}}]:
            with self.assertRaises(BackendError):
                CodexRunner.parse_events(json.dumps(event), lambda _: None)

    def test_adapter_new_and_resume_flags_and_stdin(self):
        runner = object.__new__(CodexRunner)
        runner.config = self.config
        runner.features = REQUIRED_FEATURES
        sid = str(uuid.uuid4())
        args = runner.arguments(Path("reply.txt"), sid)
        self.assertIn("read-only", args)
        self.assertIn("features.shell_tool=false", args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertEqual(args[args.index("resume") + 1], sid)
        self.assertEqual(args[-1], "-")
        self.assertNotIn("--last", args)

    def test_real_subprocess_boundary_with_fake_cli_results(self):
        runner = object.__new__(CodexRunner)
        runner.config = self.config
        runner.features = REQUIRED_FEATURES
        sid = str(uuid.uuid4())
        def invoke(args, **kwargs):
            self.assertEqual(kwargs["cwd"], self.config.workspace)
            self.assertEqual(json.loads(kwargs["input"])["body"], "question $(evil)")
            Path(args[args.index("--output-last-message") + 1]).write_text("answer", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, json.dumps({"type": "thread.started", "thread_id": sid}) + '\n{"type":"turn.completed"}', "")
        runner.invoke = invoke
        seen = []
        self.assertEqual(runner.generate("[GPT]", "question $(evil)", None, seen.append), "answer")
        self.assertEqual(seen, [sid])

    def test_config_literal_env_and_reject_bot_as_sender(self):
        path = Path(self.temp.name) / ".env"
        path.write_text("EMAIL_ADDRESS=bot@receiver.test\nEMAIL_PASSWORD='literal$()#value'\nALLOWED_SENDERS=bot@receiver.test\n", encoding="utf-8")
        self.assertEqual(read_env(path)["EMAIL_PASSWORD"], "literal$()#value")
        with self.assertRaises(ValueError):
            Config.load(path)

    def test_end_to_end_with_real_subprocess_fixture(self):
        runner = object.__new__(CodexRunner)
        runner.config = self.config
        runner.features = REQUIRED_FEATURES
        fixture = Path(__file__).with_name("fake_cli.py").resolve()
        def invoke(args, **kwargs):
            return subprocess.run([sys.executable, str(fixture), *args],
                                  env=runner.environment(), encoding="utf-8", shell=False, **kwargs)
        runner.invoke = invoke
        self.service.backend = runner
        self.assertEqual(self.service.process(incoming(body="中文与 $() 保持原样")), "sent")
        row = self.db.message(self.config.email, "<one@sender.test>")
        sid = self.db.conversation(row["thread_key"])["session_id"]
        followup = incoming("<two@sender.test>", refs=[row["reply_id"]], body="Continue")
        self.assertEqual(self.service.process(followup), "sent")
        self.assertEqual(self.db.conversation(row["thread_key"])["session_id"], sid)
        reply = BytesParser(policy=policy.default).parsebytes(self.smtp.sent[0][1])
        self.assertIn("中文与 $() 保持原样", reply.get_content())

    def test_transient_cli_error_can_recover(self):
        sid = str(uuid.uuid4())
        events = '\n'.join(json.dumps(e) for e in [
            {"type": "thread.started", "thread_id": sid},
            {"type": "error", "message": "Reconnecting"},
            {"type": "turn.completed"}])
        self.assertEqual(CodexRunner.parse_events(events, lambda _: None), sid)

    def test_imap_fetches_peek_with_gmail_id_without_marking_seen(self):
        from unittest.mock import Mock
        raw = incoming().headers.as_bytes()
        fake = Mock()
        fake.capabilities = (b"IMAP4rev1", b"X-GM-EXT-1")
        def uid(*args):
            if args[0] == "search":
                return "OK", [b"5"]
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"5 (RFC822.SIZE 900)"]
            if args[0] == "fetch":
                self.assertIn("BODY.PEEK[]", args[-1])
                return "OK", [(b"5 (X-GM-THRID 18446744073709551610)", raw), b")"]
            return "OK", [b""]
        fake.uid.side_effect = uid
        client = IMAPClient(self.config)
        client.client = fake
        messages = list(client.unread())
        self.assertEqual(messages[0].gmail_thread, "18446744073709551610")
        self.assertFalse(any(call.args[0] == "store" for call in fake.uid.call_args_list))
        client.mark_seen("5")
        self.assertEqual(fake.uid.call_args.args[0], "store")

    def test_imap_size_limit_prevents_body_download(self):
        from unittest.mock import Mock
        fake = Mock()
        fake.capabilities = ()
        fake.uid.side_effect = [("OK", [b"1"]), ("OK", [b"1 (RFC822.SIZE 999999999)"])]
        client = IMAPClient(self.config)
        client.client = fake
        self.assertEqual(list(client.unread()), [])
        self.assertEqual(fake.uid.call_count, 2)

    def test_smtp_starttls_before_login_and_explicit_envelope(self):
        from unittest.mock import Mock
        fake = Mock()
        fake.sendmail.return_value = {}
        with patch("mail_gpt.mail.smtplib.SMTP", return_value=fake):
            client = SMTPClient(replace(self.config, smtp_security="starttls", smtp_port=587))
            client.send("me@sender.test", b"raw")
        calls = [call[0] for call in fake.method_calls]
        self.assertLess(calls.index("starttls"), calls.index("login"))
        fake.sendmail.assert_called_once_with(self.config.email, ["me@sender.test"], b"raw")
        fake.close.assert_called_once()

    def test_read_qq_reply_is_not_lost_before_poll(self):
        from unittest.mock import Mock
        self.service.process(incoming())
        row = self.db.message(self.config.email, "<one@sender.test>")
        reply = incoming("<follow@sender.test>", subject="Re: [GPT]", refs=[row["reply_id"]], body="Follow-up\n\n---Original---\nFrom: bot\nOld text")
        fake = Mock()
        fake.capabilities = ("IMAP4REV1", "X-GM-EXT-1")
        def uid(*args):
            if args[0] == "search":
                # The real QQ replies have already become Seen in Gmail.
                return "OK", [b"" if "UNSEEN" in str(args) else b"196"]
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"196 (RFC822.SIZE 900)"]
            return "OK", [(b"196 (X-GM-THRID 12345678901234567890)", reply.headers.as_bytes()), b")"]
        fake.uid.side_effect = uid
        mailbox = IMAPClient(self.config)
        mailbox.client = fake
        candidates = list(mailbox.unread())
        self.assertEqual(len(candidates), 1, "Read QQ follow-up must still reach the worker")
        self.assertEqual(self.service.process(candidates[0]), "sent")
        self.assertEqual(self.backend.calls[-1][1], "Follow-up")
        self.assertIsNotNone(self.backend.calls[-1][2])

    def test_gmail_capabilities_use_stdlib_string_representation(self):
        from unittest.mock import Mock
        fake = Mock()
        fake.capabilities = ("IMAP4REV1", "X-GM-EXT-1")
        def uid(*args):
            if args[0] == "search":
                return "OK", [b"1"]
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"1 (RFC822.SIZE 900)"]
            self.assertIn("X-GM-THRID", args[-1])
            return "OK", [(b"1 (X-GM-THRID 18446744073709551610)", incoming().headers.as_bytes()), b")"]
        fake.uid.side_effect = uid
        mailbox = IMAPClient(self.config)
        mailbox.client = fake
        self.assertEqual(list(mailbox.unread())[0].gmail_thread, "18446744073709551610")

    def test_gpt_tag_needs_no_trailing_space(self):
        self.assertEqual(command("[GPT]测试"), "chat")
        self.assertEqual(command("回复：[GPT:NEW]新问题"), "new")

    def test_qq_original_marker_removed_from_reply(self):
        mail = incoming(refs=["<old@sender.test>"], body="Follow-up\n\n---Original---\nFrom: bot\nOld answer")
        self.assertEqual(mail.body, "Follow-up")

    def test_scan_start_persists_across_restarts(self):
        with patch("mail_gpt.storage.time.time", return_value=1788707700):
            first = self.db.scan_start(self.config.email)
        with patch("mail_gpt.storage.time.time", return_value=1789907700):
            self.assertEqual(self.db.scan_start(self.config.email), first)
        self.assertEqual(self.db.scan_start(self.config.email, "2026-09-01"), "2026-09-01")

    def test_legacy_gmail_mapping_keeps_latest_answered_session(self):
        first = incoming(thread=None)
        second = incoming("<two@sender.test>", thread=None)
        self.service.process(first)
        self.service.process(second)
        newer = self.db.message(self.config.email, second.message_id)
        first.gmail_thread = second.gmail_thread = "123"
        self.db.backfill_gmail_aliases(self.config.email, [first, second])
        self.assertEqual(self.db.resolve(self.config.email, incoming("<reply@sender.test>", thread="123")), newer["thread_key"])
        self.db.backfill_gmail_aliases(self.config.email, [first])
        self.assertEqual(self.db.resolve(self.config.email, incoming("<reply2@sender.test>", thread="123")), newer["thread_key"])

    def test_status_reports_unrecorded_read_mail_without_writing(self):
        from mail_gpt.status import inspect_status
        from unittest.mock import MagicMock
        self.service.process(incoming())
        self.db.scan_start(self.config.email)
        missing = incoming("<missing@sender.test>", subject="Re: [GPT]")
        mailbox = MagicMock()
        mailbox.__enter__.return_value = mailbox
        mailbox.candidates.return_value = [incoming(), missing]
        with patch("mail_gpt.status.IMAPClient"):
            report = inspect_status(self.config, mailbox_factory=lambda *a, **kw: mailbox)
        self.assertEqual(report["mailbox_counts"], {"sent": 1, "pending-not-recorded": 1})
        self.assertIsNone(self.db.message(self.config.email, missing.message_id))
        mailbox.mark_seen.assert_not_called()

    def test_sent_candidate_is_not_counted_as_new_send(self):
        mail = incoming()
        self.assertEqual(self.service.process(mail), "sent")
        self.assertEqual(self.service.process(mail), "already-sent")
        self.assertEqual(len(self.smtp.sent), 1)


if __name__ == "__main__":
    unittest.main()

from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import uuid

from mail_gpt.codex import BackendError, CodexRunner, REQUIRED_FEATURES
from mail_gpt.config import Config
from mail_gpt.service import Service
from mail_gpt.storage import Store
from tests.test_bot import FakeSMTP, incoming


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config('bot@receiver.test', 'secret', frozenset({'me@sender.test'}),
                             root / 'db', root / 'home', root / 'empty')
        self.sid = str(uuid.uuid4())
        self.runner = object.__new__(CodexRunner)
        self.runner.config = self.config
        self.runner.features = REQUIRED_FEATURES

    def events(self, tool='web_search'):
        return '\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started', 'thread_id': self.sid},
            {'type': 'item.started', 'item': {'id': 'item_0', 'type': tool}},
            {'type': 'item.completed', 'item': {'id': 'item_0', 'type': tool,
                'action': {'type': 'search', 'query': 'public facts'}}},
            {'type': 'turn.completed'}])

    def test_only_search_is_permitted_in_live_mode(self):
        self.assertEqual(CodexRunner.parse_events(self.events(), lambda _: None,
                         allow_web_search=True), self.sid)
        for tool in ('command_execution', 'file_change', 'mcp_tool_call', 'dynamic_tool_call'):
            with self.subTest(tool=tool), self.assertRaises(BackendError):
                CodexRunner.parse_events(self.events(tool), lambda _: None, allow_web_search=True)

    def test_disabled_mode_rejects_search(self):
        with self.assertRaises(BackendError):
            CodexRunner.parse_events(self.events(), lambda _: None)

    def test_new_and_resumed_turns_keep_isolation_in_both_modes(self):
        for mode in ('live', 'disabled'):
            self.runner.config = replace(self.config, web_search=mode)
            for sid in (None, self.sid):
                args = self.runner.arguments(Path('reply.txt'), sid)
                self.assertIn('web_search=' + json.dumps(mode), args)
                self.assertIn('approval_policy="never"', args)
                self.assertIn('--ignore-user-config', args)
                self.assertIn('read-only', args)
                for feature in REQUIRED_FEATURES:
                    enabled = feature == 'code_mode_host' and mode == 'live'
                    self.assertIn('features.' + feature + '=' + str(enabled).lower(), args)

    def test_search_answer_urls_survive_delivery_and_session_resume(self):
        answer = '查询结果。\n来源：https://example.com/company?a=1&b=2'
        sessions = []
        def invoke(args, **kwargs):
            sessions.append(args[args.index('resume') + 1] if 'resume' in args else None)
            Path(args[args.index('--output-last-message') + 1]).write_text(answer, encoding='utf-8')
            return subprocess.CompletedProcess(args, 0, self.events(), '')
        self.runner.invoke = invoke
        db = Store(self.config.database)
        self.addCleanup(db.close)
        smtp = FakeSMTP()
        service = Service(self.config, db, self.runner, smtp)
        first = incoming(body='请搜索公司')
        self.assertEqual(service.process(first), 'sent')
        row = db.message(self.config.email, first.message_id)
        second = incoming('<second@sender.test>', refs=[row['reply_id']], body='继续搜索')
        self.assertEqual(service.process(second), 'sent')
        self.assertEqual(sessions, [None, self.sid])
        self.assertEqual(service.process(first), 'already-sent')
        self.assertEqual(len(smtp.sent), 2)
        for _, raw in smtp.sent:
            mail = BytesParser(policy=policy.default).parsebytes(raw)
            self.assertEqual(answer, mail.get_content().replace('\r\n', '\n').strip())

    def test_config_default_and_explicit_search_modes(self):
        path = Path(self.temp.name) / '.env'
        self.assertEqual(Config.load(path, require_mail=False).web_search, 'live')
        for mode in ('disabled', 'live'):
            path.write_text('CODEX_WEB_SEARCH=' + mode, encoding='utf-8')
            self.assertEqual(Config.load(path, require_mail=False).web_search, mode)
        path.write_text('CODEX_WEB_SEARCH=unrestricted', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'CODEX_WEB_SEARCH'):
            Config.load(path, require_mail=False)

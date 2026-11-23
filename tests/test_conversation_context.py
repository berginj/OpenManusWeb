import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.web import app as web


class ConversationContextTests(unittest.TestCase):
    def test_context_is_ordered_bounded_and_can_load_saved_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / 'job_test'
            task.mkdir()
            saved = {'status': 'completed', 'workspace': 'job_test', 'prompt': 'first',
                     'result': 'A' * 5000, 'previous_session_id': None}
            active = {'next': {'status': 'error', 'workspace': 'job_test', 'prompt': 'second',
                               'result': 'failed', 'previous_session_id': 'old'}}
            with patch.object(web, 'WORKSPACE_ROOT', root), patch.object(web, 'active_sessions', active), \
                    patch.object(web.agent_monitor, 'load_report', return_value=saved):
                exchanges = web.previous_exchanges('next', task)
            self.assertEqual([x['session_id'] for x in exchanges], ['old', 'next'])
            self.assertIn('Middle content truncated', exchanges[0]['result'])
            self.assertEqual(exchanges[1]['status'], 'error')
            prompt = web.prompt_with_context('fix it', exchanges)
            self.assertIn('first', prompt)
            self.assertIn('Current user request:\nfix it', prompt)

    def test_context_rejects_another_workspace_or_running_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / 'job_one'
            task.mkdir()
            sessions = {'other': {'status': 'completed', 'workspace': 'job_two'},
                        'running': {'status': 'processing', 'workspace': 'job_one'}}
            with patch.object(web, 'WORKSPACE_ROOT', root), patch.object(web, 'active_sessions', sessions):
                for sid, status in [('other', 400), ('running', 409)]:
                    with self.assertRaises(HTTPException) as raised:
                        web.previous_exchanges(sid, task)
                    self.assertEqual(raised.exception.status_code, status)

    def test_empty_context_leaves_simple_prompt_unchanged(self):
        self.assertEqual(web.prompt_with_context('hello', []), 'hello')


if __name__ == '__main__':
    unittest.main()

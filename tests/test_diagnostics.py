import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from loguru import logger

from app.web import app as web
from app.web.log_handler import capture_session_logs, clear_logs, get_logs


class DiagnosticsTests(unittest.TestCase):
    def test_task_initialization_failure_is_saved_with_traceback(self):
        sid = 'failure-test'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, logs = root / 'workspace', root / 'logs'
            (workspace / 'job_test').mkdir(parents=True)
            logs.mkdir()
            session = {'status': 'processing', 'prompt': 'test', 'result': None,
                       'workspace': 'job_test', 'project_path': None,
                       'model': web.config.llm['default'].model}
            with patch.object(web, 'WORKSPACE_ROOT', workspace), patch.object(web, 'LOGS_DIR', logs), \
                    patch.object(web, 'active_sessions', {sid: session}), \
                    patch.object(web, 'cancel_events', {sid: asyncio.Event()}), \
                    patch.object(web.agent_monitor, 'DB', root / 'metrics.sqlite'), \
                    patch.object(web.agent_monitor, 'RUNS', {}), \
                    patch.object(web, 'Manus', side_effect=RuntimeError('agent initialization failed')):
                web.agent_monitor.begin_task(sid, session['model'])
                asyncio.run(web.process_prompt(sid, 'test'))
                report = web.agent_monitor.load_report(sid)
                self.assertEqual(report['status'], 'error')
                self.assertIn('RuntimeError: agent initialization failed', report['error_traceback'])
                self.assertEqual(web.agent_monitor.history()[0]['status'], 'error')
            web.ThinkingTracker.clear_session(sid)

    def test_runtime_environment_omits_endpoint_credentials_and_query(self):
        settings = SimpleNamespace(base_url='http://alice:secret@localhost:11434/v1?token=secret',
                                   api_type='ollama', max_tokens=123, temperature=0,
                                   request_timeout_seconds=600)
        with patch.object(web, 'config', SimpleNamespace(llm={'default': settings})):
            env = web.runtime_environment()
        self.assertEqual(env['endpoint_origin'], 'http://localhost:11434')
        self.assertNotIn('secret', str(env))
        self.assertNotIn('alice', str(env))
        self.assertNotIn('/v1', str(env))

    def test_model_selection_reports_backend_outage(self):
        request = web.SessionRequest(prompt='test', model='different-model')
        offline = {'models': [], 'loaded': [], 'error': 'connection refused'}
        with patch.object(web.agent_monitor, 'RUNS', {}), \
                patch.object(web.agent_monitor, 'ollama_models', return_value=offline):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(web.create_chat_session(request, BackgroundTasks()))
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn('unreachable', raised.exception.detail)

    def test_monitor_uses_saved_metrics_when_task_is_not_in_memory(self):
        report = {'metrics': {'status': 'interrupted', 'elapsed': 42},
                  'progress': {'current_step': 'Reviewing task', 'total_steps': 0}}
        with patch.object(web.agent_monitor, 'ollama_models', return_value={'models': [], 'loaded': [], 'error': None}), \
                patch.object(web.agent_monitor, 'snapshot', return_value=None), \
                patch.object(web.agent_monitor, 'load_report', return_value=report), \
                patch.object(web.agent_monitor, 'history', return_value=[]):
            response = asyncio.run(web.agent_monitor_api('saved-task'))
        self.assertEqual(response['run']['status'], 'interrupted')
        self.assertEqual(response['progress']['current_step'], 'Reviewing task')

    def test_older_warning_is_kept_in_diagnostics_and_recent_logs_are_bounded(self):
        sid = 'diagnostic-test'
        session = {'status': 'processing', 'prompt': 'test task', 'result': None,
                   'workspace': 'test', 'project_path': None}
        entries = [{'level': 'ERROR', 'message': 'original failure'}] + [
            {'level': 'INFO', 'message': 'x' * 3000} for _ in range(35)]
        with patch.dict(web.active_sessions, {sid: session}), patch.object(web, 'get_logs', return_value=entries):
            report = asyncio.run(web.get_chat_diagnostics(sid))
        self.assertEqual(report['important_logs'][0]['message'], 'original failure')
        self.assertEqual(len(report['recent_session_logs']), 30)
        self.assertIn('characters omitted', report['recent_session_logs'][0]['message'])

    def test_capture_preserves_log_level(self):
        sid = 'capture-test'
        clear_logs(sid)
        with capture_session_logs(sid):
            logger.warning('warning for diagnostics')
        entries = get_logs(sid)
        self.assertTrue(any(entry.get('level') == 'WARNING' and 'warning for diagnostics' in entry['message'] for entry in entries))
        clear_logs(sid)


if __name__ == '__main__':
    unittest.main()

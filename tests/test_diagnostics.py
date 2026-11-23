import asyncio
import json
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
    def test_websocket_waits_for_cleanup_before_sending_result(self):
        sid = 'websocket-cleanup-test'
        session = {'status': 'processing', 'prompt': 'test', 'result': None,
                   'workspace': 'test', 'project_path': None, 'log': [],
                   'model': web.config.llm['default'].model}

        class Socket:
            def __init__(self):
                self.messages = []
            async def accept(self):
                pass
            async def send_text(self, data):
                self.messages.append(json.loads(data))
            async def close(self):
                pass

        async def exercise():
            started, release = asyncio.Event(), asyncio.Event()
            async def task_body(*args):
                session['status'], session['result'] = 'completed', 'done'
                started.set()
                await release.wait()
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(web, 'active_sessions', {sid: session}), \
                    patch.object(web, 'LOGS_DIR', Path(directory)), \
                    patch.object(web.agent_monitor, 'RUNS', {}), \
                    patch.object(web.agent_monitor, 'DB', Path(directory) / 'metrics.sqlite'), \
                    patch.object(web, '_process_prompt', task_body):
                web.agent_monitor.begin_task(sid, session['model'])
                task = asyncio.create_task(web.process_prompt(sid, 'test'))
                await started.wait()
                socket = Socket()
                connection = asyncio.create_task(web.websocket_endpoint(socket, sid))
                await asyncio.sleep(0.3)
                self.assertTrue(socket.messages)
                self.assertTrue(all(message.get('status') == 'processing' for message in socket.messages))
                self.assertTrue(all(not message.get('result') for message in socket.messages))
                release.set()
                await task
                await asyncio.wait_for(connection, 2)
                self.assertEqual(socket.messages[-1]['status'], 'completed')
                self.assertEqual(socket.messages[-1]['result'], 'done')
        asyncio.run(exercise())

    def test_terminal_status_waits_for_cleanup(self):
        sid = 'cleanup-test'
        session = {'status': 'processing', 'prompt': 'test', 'result': None,
                   'workspace': 'test', 'project_path': None,
                   'model': web.config.llm['default'].model}

        async def exercise():
            cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()

            async def task_body(*args):
                session['status'], session['result'] = 'completed', 'done'
                cleanup_started.set()
                await release_cleanup.wait()

            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(web, 'active_sessions', {sid: session}), \
                    patch.object(web.agent_monitor, 'RUNS', {}), \
                    patch.object(web.agent_monitor, 'DB', Path(directory) / 'metrics.sqlite'), \
                    patch.object(web, '_process_prompt', task_body):
                web.agent_monitor.begin_task(sid, session['model'])
                task = asyncio.create_task(web.process_prompt(sid, 'test'))
                await cleanup_started.wait()
                response = await web.get_chat_result(sid)
                self.assertEqual(response['status'], 'processing')
                self.assertIsNone(response['result'])
                self.assertEqual((await web.get_chat_diagnostics(sid))['status'], 'processing')
                with self.assertRaises(HTTPException) as raised:
                    await web.create_chat_session(web.SessionRequest(prompt='next'), BackgroundTasks())
                self.assertEqual(raised.exception.status_code, 409)
                release_cleanup.set()
                await task
                response = await web.get_chat_result(sid)
                self.assertEqual(response['status'], 'completed')
                self.assertEqual(response['result'], 'done')
                self.assertEqual(web.agent_monitor.load_report(sid)['status'], 'completed')

        asyncio.run(exercise())

    def test_failed_task_saves_workspace_changes(self):
        sid = 'changes-test'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, logs = root / 'workspace', root / 'logs'
            project = workspace / 'job_test'
            (project / 'nested').mkdir(parents=True)
            (project / 'nested' / 'existing').write_text('before')
            logs.mkdir()
            session = {'status': 'processing', 'prompt': 'test', 'result': None,
                       'workspace': 'job_test', 'project_path': None, 'requested_mode': 'direct',
                       'model': web.config.llm['default'].model}

            class FailingAgent:
                async def run(self, *args, **kwargs):
                    (project / 'nested' / 'existing').write_text('after longer')
                    (project / 'nested' / 'new').write_text('created')
                    raise RuntimeError('failed after writing')

            with patch.object(web, 'WORKSPACE_ROOT', workspace), patch.object(web, 'LOGS_DIR', logs), \
                    patch.object(web, 'active_sessions', {sid: session}), \
                    patch.object(web, 'cancel_events', {sid: asyncio.Event()}), \
                    patch.object(web.agent_monitor, 'DB', root / 'metrics.sqlite'), \
                    patch.object(web.agent_monitor, 'RUNS', {}), patch.object(web, 'Manus', FailingAgent):
                web.agent_monitor.begin_task(sid, session['model'])
                asyncio.run(web.process_prompt(sid, 'test'))
                report = web.agent_monitor.load_report(sid)
                self.assertEqual(report['status'], 'error')
                self.assertEqual(report['workspace_changes']['added']['paths'], ['nested/new'])
                self.assertEqual(report['workspace_changes']['modified']['paths'], ['nested/existing'])
            web.ThinkingTracker.clear_session(sid)

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

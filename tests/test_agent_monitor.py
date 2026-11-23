import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.web import agent_monitor as monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in [('DB', Path(self.directory.name) / 'metrics.sqlite'), ('RUNS', {})]:
            patcher = patch.object(monitor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_estimates_require_matching_completed_tasks(self):
        for index, seconds in enumerate((10, 20, 30)):
            sid = str(index)
            monitor.begin_task(sid, 'model-a')
            monitor.set_review(sid, 'simple', 'review', 'hello')
            monitor.RUNS[sid]['started_at'] = time.time() - seconds
            monitor.finish_task(sid, 'completed')
        estimate = monitor.estimate('model-a', 'simple', 'greeting')
        self.assertEqual((estimate['sample_size'], estimate['median_seconds']), (3, 20))
        self.assertEqual(monitor.estimate('model-a', 'simple', 'coding')['sample_size'], 0)
        self.assertEqual(monitor.estimate('model-b', 'simple', 'greeting')['sample_size'], 0)
        self.assertEqual(monitor.estimate('model-a', 'complex', 'greeting')['sample_size'], 0)

    def test_checkpoint_marks_interrupted_after_process_state_is_gone(self):
        monitor.begin_task('task', 'model')
        monitor.save_report('task', {'status': 'processing', 'prompt': 'work',
                                     'metrics': {'status': 'processing'}})
        self.assertEqual(monitor.load_report('task')['status'], 'processing')
        monitor.RUNS.clear()
        report = monitor.load_report('task')
        self.assertEqual(report['status'], 'interrupted')
        self.assertEqual(report['metrics']['status'], 'interrupted')
        self.assertGreater(report['checkpoint_at'], 0)
        self.assertIn('not resumed', report['note'])
        self.assertEqual(monitor.recent_reports()[0]['status'], 'interrupted')

    def test_reports_are_searchable_and_cursor_paginates_ties(self):
        for i in range(55):
            monitor.save_report(f'task-{i:03}', {'status': 'completed',
                                'prompt': 'Report 100%_done!' if i == 0 else f'task number {i}',
                                'workspace': 'Project Alpha' if i == 1 else 'other'})
        connection = monitor._conn()
        try:
            with connection:
                connection.execute('UPDATE reports SET saved_at = 123')
        finally:
            connection.close()
        first = monitor.recent_reports(20)
        second = monitor.recent_reports(20, first[-1]['saved_at'], first[-1]['session_id'])
        third = monitor.recent_reports(20, second[-1]['saved_at'], second[-1]['session_id'])
        self.assertEqual(len(first) + len(second) + len(third), 55)
        self.assertEqual(len({x['session_id'] for x in first + second + third}), 55)
        self.assertEqual(monitor.recent_reports(query='100%_done!')[0]['session_id'], 'task-000')
        self.assertEqual(monitor.recent_reports(query='project alpha')[0]['session_id'], 'task-001')
        self.assertEqual(monitor.recent_reports(query='task-054')[0]['session_id'], 'task-054')
        self.assertEqual(monitor.recent_reports(query='no-such-task'), [])

    def test_broader_estimate_and_original_forecast_are_saved(self):
        for index, seconds in enumerate((10, 20, 30, 40, 50)):
            sid = f'previous-{index}'
            monitor.begin_task(sid, 'model-a')
            monitor.set_review(sid, 'complex', 'review', 'Research a topic')
            monitor.RUNS[sid]['started_at'] = time.time() - seconds
            monitor.finish_task(sid, 'completed')
        monitor.begin_task('next', 'model-a')
        monitor.set_review('next', 'complex', 'review', 'Debug this code')
        forecast = monitor.snapshot('next')['estimate']
        self.assertEqual(forecast['basis'], 'all categories, same complexity')
        self.assertEqual(forecast['sample_size'], 5)
        self.assertEqual(forecast['median_seconds'], 30)
        monitor.finish_task('next', 'completed')
        row = monitor.history()[0]
        self.assertEqual(row['forecast_median'], 30)
        self.assertEqual(row['forecast_basis'], 'all categories, same complexity')

    def test_unclassified_history_is_reference_not_forecast(self):
        for index, seconds in enumerate((300, 1600)):
            sid = f'legacy-{index}'
            monitor.begin_task(sid, 'model-a')
            monitor.RUNS[sid]['started_at'] = time.time() - seconds
            monitor.finish_task(sid, 'completed')
        monitor.begin_task('current', 'model-a')
        monitor.set_review('current', 'complex', 'review', 'write code')
        estimate = monitor.snapshot('current')['estimate']
        self.assertNotIn('median_seconds', estimate)
        self.assertEqual(estimate['sample_size'], 0)
        self.assertEqual(estimate['reference'], {'sample_size': 2, 'low_seconds': 300, 'high_seconds': 1600})
        monitor.finish_task('current', 'completed')
        self.assertIsNone(monitor.history()[0]['forecast_median'])
        self.assertNotIn('reference', monitor.estimate('other-model', 'complex', 'coding'))

    def test_usage_coverage_and_estimates_are_recorded_separately(self):
        monitor.begin_task('task', 'model')
        token = monitor.enter_task('task')
        self.addCleanup(monitor.leave_task, token)
        call = monitor.start_call('model')
        monitor.end_call(call, SimpleNamespace(prompt_tokens=0, completion_tokens=8))
        call = monitor.start_call('model')
        monitor.end_call(call, None, 'abcdefghijklmnop')
        call = monitor.start_call('model')
        monitor.fail_call(call)
        run = monitor.snapshot('task')
        self.assertEqual((run['calls'], run['completed_calls'], run['errors']), (3, 2, 1))
        self.assertEqual((run['input_reported_calls'], run['output_reported_calls']), (1, 1))
        self.assertEqual((run['prompt_tokens'], run['output_tokens'], run['estimated_tokens']), (0, 8, 4))
        monitor.finish_task('task', 'completed')
        row = monitor.history()[0]
        self.assertEqual((row['input_reported_calls'], row['output_reported_calls']), (1, 1))
        self.assertGreaterEqual(row['measured_output_seconds'], 0)

    def test_active_tool_is_visible_and_cleared(self):
        monitor.begin_task('task', 'model')
        token = monitor.enter_task('task')
        try:
            tool = monitor.start_tool('python_execute', '{"code":"print(55)"}')
            snapshot = monitor.snapshot('task')
            self.assertEqual(snapshot['stage'], 'tool_processing')
            self.assertEqual(snapshot['active_tool']['name'], 'python_execute')
            self.assertIn('print(55)', snapshot['tool_events'][-1]['arguments'])
            self.assertGreaterEqual(snapshot['active_tool']['in_flight_seconds'], 0)
            monitor.end_tool(tool, '55')
            self.assertIsNone(monitor.snapshot('task')['active_tool'])
            self.assertEqual(monitor.snapshot('task')['tool_events'][-1]['result'], '55')
            self.assertEqual(monitor.snapshot('task')['tool_events'][-1]['state'], 'returned')
        finally:
            monitor.leave_task(token)

    def test_tool_event_history_and_content_are_bounded(self):
        monitor.begin_task('task', 'model')
        token = monitor.enter_task('task')
        try:
            for i in range(15):
                tool = monitor.start_tool(str(i), 'a' * 2000)
                monitor.end_tool(tool, 'b' * 2000)
            events = monitor.snapshot('task')['tool_events']
            self.assertEqual(len(events), 12)
            self.assertEqual(events[0]['name'], '3')
            self.assertIn('400 characters omitted', events[-1]['arguments'])
            self.assertIn('400 characters omitted', events[-1]['result'])
        finally:
            monitor.leave_task(token)

    def test_model_discovery_recovers_and_keeps_endpoint_caches_separate(self):
        clock = [100.0]
        online = [False]
        requests = []

        def get_json(url):
            requests.append(url)
            if not online[0]:
                raise OSError('offline')
            if url.endswith('/api/tags'):
                return {'models': [{'name': 'model-a'}]}
            return {'models': []}

        initial = {'at': 0.0, 'base_url': None, 'models': [], 'loaded': [], 'error': None}
        with patch.object(monitor, 'OLLAMA_CACHE', initial), \
                patch.object(monitor, '_get_json', side_effect=get_json), \
                patch.object(monitor.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertIn('offline', monitor.ollama_models('http://localhost:11434/v1')['error'])
            online[0] = True
            clock[0] += 2
            self.assertIn('offline', monitor.ollama_models('http://localhost:11434/v1')['error'])
            clock[0] += 2
            self.assertEqual(monitor.ollama_models('http://localhost:11434/v1')['models'], ['model-a'])
            count = len(requests)
            monitor.ollama_models('http://localhost:11434/v1')
            self.assertEqual(len(requests), count)
            monitor.ollama_models('http://localhost:11434/v1', force=True)
            self.assertGreater(len(requests), count)
            monitor.ollama_models('http://localhost:11435/v1')
            self.assertIn('http://localhost:11435/api/tags', requests)

    def test_cached_model_load_reports_unknown_separately_from_unloaded(self):
        cached = {'base_url': 'http://localhost:11434', 'error': None,
                  'loaded_error': None, 'observed_at': 100,
                  'loaded_details': [{'name': 'model-a', 'vram_bytes': 1024, 'size_bytes': 2048}]}
        with patch.object(monitor, 'OLLAMA_CACHE', cached):
            loaded = monitor.cached_model_status('model-a', 'http://localhost:11434/v1')
            self.assertEqual(loaded['state'], 'loaded')
            self.assertEqual(loaded['details']['vram_bytes'], 1024)
            self.assertEqual(monitor.cached_model_status('model-b', 'http://localhost:11434')['state'], 'not loaded')
            self.assertEqual(monitor.cached_model_status('model-a', 'http://other:11434')['state'], 'unknown')
            cached['loaded_error'] = 'TimeoutError'
            self.assertEqual(monitor.cached_model_status('model-a', 'http://localhost:11434')['state'], 'unknown')

    def test_model_discovery_keeps_only_relevant_load_details(self):
        def get_json(url):
            if url.endswith('/api/tags'):
                return {'models': [{'name': 'model-a'}]}
            return {'models': [{'name': 'model-a', 'size': 2048, 'size_vram': 1024,
                                'context_length': 4096, 'digest': 'private-not-needed',
                                'details': {'quantization_level': 'Q4'}}]}
        initial = {'at': 0.0, 'base_url': None, 'models': [], 'loaded': [], 'error': None}
        with patch.object(monitor, 'OLLAMA_CACHE', initial), patch.object(monitor, '_get_json', side_effect=get_json):
            details = monitor.ollama_models('http://localhost:11434/v1')
            status = monitor.cached_model_status('model-a', 'http://localhost:11434')
        self.assertEqual(details['loaded'], ['model-a'])
        self.assertEqual(status['details']['context_length'], 4096)
        self.assertEqual(status['details']['quantization'], 'Q4')
        self.assertNotIn('digest', status['details'])


if __name__ == '__main__':
    unittest.main()

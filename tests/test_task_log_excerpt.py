import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.web import app as web
from app.web.task_log_excerpt import read_log_excerpt


class TaskLogExcerptTests(unittest.TestCase):
    def test_search_keeps_context_and_bounds_the_latest_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'task.log'
            path.write_text('\n'.join('ERROR here' if i in (3, 5) else f'line {i}'
                                      for i in range(1, 11)) + '\n', encoding='utf-8')
            result = read_log_excerpt(path, 'error', 5)
            self.assertEqual(result['matches'], 2)
            self.assertEqual([item['number'] for item in result['entries']], [3, 4, 5, 6, 7])
            self.assertEqual(result['omitted_entries'], 2)
            recent = read_log_excerpt(path, '', 3)
            self.assertEqual([item['number'] for item in recent['entries']], [8, 9, 10])
            self.assertEqual(recent['omitted_entries'], 7)

    def test_long_entry_is_shortened_but_retains_both_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'task.log'
            path.write_text('begin ' + 'x' * 7000 + ' END-MARKER\n', encoding='utf-8')
            entry = read_log_excerpt(path, 'end-marker')['entries'][0]['text']
            self.assertTrue(entry.startswith('begin '))
            self.assertTrue(entry.endswith('END-MARKER'))
            self.assertIn('characters omitted', entry)
            self.assertLess(len(entry), 4100)

    def test_endpoint_is_scoped_to_the_selected_task(self):
        sid = '12345678-1234-1234-1234-123456789abc'
        other = 'abcdefab-1234-1234-1234-123456789abc'
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            (logs / 'job_12345678.log').write_text('only this task\n', encoding='utf-8')
            (logs / 'job_abcdefab.log').write_text('another task\n', encoding='utf-8')
            sessions = {sid: {'status': 'completed'}}
            with patch.object(web, 'LOGS_DIR', logs), patch.object(web, 'active_sessions', sessions), \
                    patch.object(web.agent_monitor, 'load_report', return_value=None):
                result = asyncio.run(web.task_log_excerpt(sid, web.LogExcerptRequest()))
                self.assertEqual(result['entries'][0]['text'], 'only this task')
                with self.assertRaises(HTTPException) as missing:
                    asyncio.run(web.task_log_excerpt(other, web.LogExcerptRequest()))
                self.assertEqual(missing.exception.status_code, 404)
                with self.assertRaises(HTTPException) as invalid:
                    asyncio.run(web.task_log_excerpt('../other', web.LogExcerptRequest()))
                self.assertEqual(invalid.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()

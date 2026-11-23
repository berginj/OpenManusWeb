import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from app.tool.python_execute import PythonExecute


class PythonExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_error_and_server_stdout_are_isolated(self):
        original = sys.stdout
        tool = PythonExecute()
        result = await tool.execute("print('hello')")
        self.assertEqual(result['observation'], 'hello\n')
        error = await tool.execute("print('before'); raise ValueError('bad input')")
        self.assertFalse(error['success'])
        self.assertIn('before', error['observation'])
        self.assertIn('ValueError: bad input', error['observation'])
        self.assertIs(sys.stdout, original)

    async def test_timeout_terminates_direct_worker_and_does_not_block_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'late-write.txt'
            code = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('late')"
            task = asyncio.create_task(PythonExecute().execute(code, timeout=0.2))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            result = await task
            self.assertFalse(result['success'])
            self.assertIn('timeout', result['observation'])
            await asyncio.sleep(1.1)
            self.assertFalse(marker.exists())

    async def test_cancellation_reaps_direct_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'canceled-write.txt'
            code = f"import time; time.sleep(1); open({str(marker)!r}, 'w').write('late')"
            task = asyncio.create_task(PythonExecute().execute(code, timeout=5))
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(1.1)
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()

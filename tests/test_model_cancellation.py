import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.llm import LLM, _await_model_operation
from app.agent.toolcall import ToolCallAgent
from app.web import agent_monitor as monitor


class ModelCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_tool_is_cleared_when_execution_fails(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def failing_tool(agent, command):
            started.set()
            await release.wait()
            raise RuntimeError('tool failed')

        agent = ToolCallAgent()
        command = SimpleNamespace(function=SimpleNamespace(name='python_execute'))
        with patch.object(monitor, 'RUNS', {}), patch.object(ToolCallAgent, '_execute_tool', failing_tool):
            monitor.begin_task('task', 'test')
            token = monitor.enter_task('task')
            try:
                task = asyncio.create_task(agent.execute_tool(command))
                await asyncio.wait_for(started.wait(), 1)
                self.assertEqual(monitor.snapshot('task')['active_tool']['name'], 'python_execute')
                release.set()
                with self.assertRaisesRegex(RuntimeError, 'tool failed'):
                    await task
                self.assertIsNone(monitor.snapshot('task')['active_tool'])
                event = monitor.snapshot('task')['tool_events'][-1]
                self.assertEqual(event['state'], 'raised')
                self.assertIn('tool failed', event['result'])
            finally:
                monitor.leave_task(token)

    async def test_stopping_stream_closes_response(self):
        stop, waiting, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                waiting.set()
                await asyncio.Event().wait()

            async def close(self):
                closed.set()

        async def request(**kwargs):
            return Stream()

        model = object.__new__(LLM)
        model.model, model.temperature, model.max_tokens = 'test', 0, 100
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch.object(monitor, 'RUNS', {}):
            monitor.begin_task('task', 'test')
            token = monitor.enter_task('task', stop)
            try:
                task = asyncio.create_task(model.ask(messages=[{'role': 'user', 'content': 'test'}]))
                await asyncio.wait_for(waiting.wait(), 1)
                stop.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(closed.is_set())
                self.assertEqual(monitor.snapshot('task')['cancelled_calls'], 1)
            finally:
                monitor.leave_task(token)

    async def test_inflight_tool_request_is_canceled_and_tracked(self):
        stop = asyncio.Event()
        started = asyncio.Event()
        closed = asyncio.Event()

        async def request(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        model = object.__new__(LLM)
        model.model, model.temperature, model.max_tokens = 'test', 0, 100
        model.request_timeout_seconds = 600
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
        with patch.object(monitor, 'RUNS', {}):
            monitor.begin_task('task', 'test')
            token = monitor.enter_task('task', stop)
            try:
                task = asyncio.create_task(model.ask_tool(messages=[{'role': 'user', 'content': 'test'}]))
                await asyncio.wait_for(started.wait(), 1)
                stop.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(closed.is_set())
                snapshot = monitor.snapshot('task')
                self.assertIsNone(snapshot['active_call'])
                self.assertEqual((snapshot['cancelled_calls'], snapshot['errors']), (1, 0))
            finally:
                monitor.leave_task(token)

    async def test_external_timeout_cleans_up_operation(self):
        stop = asyncio.Event()
        closed = asyncio.Event()

        async def request():
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        token = monitor.enter_task('task', stop)
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(_await_model_operation(request()), 0.05)
            self.assertTrue(closed.is_set())
        finally:
            monitor.leave_task(token)


if __name__ == '__main__':
    unittest.main()

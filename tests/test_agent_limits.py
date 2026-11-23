import asyncio
import unittest

from app.agent.base import BaseAgent
from app.schema import AgentState


class EndlessAgent(BaseAgent):
    name: str = 'test'

    async def step(self):
        return 'still working'


class FinishingAgent(EndlessAgent):
    async def step(self):
        self.state = AgentState.FINISHED
        return 'done'


class AgentLimitTests(unittest.TestCase):
    def test_limit_without_finish_is_error(self):
        agent = EndlessAgent(max_steps=1)
        with self.assertRaisesRegex(RuntimeError, 'did not finish.*step limit'):
            asyncio.run(agent.run('test'))

    def test_finishing_on_last_step_is_success(self):
        agent = FinishingAgent(max_steps=1)
        self.assertIn('done', asyncio.run(agent.run('test')))


if __name__ == '__main__':
    unittest.main()

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app.web import app as web


class ExecutionModeTests(unittest.TestCase):
    def test_single_arithmetic_expression_skips_model_review(self):
        with patch.object(web, 'LLM') as model:
            for prompt in ('What is 17 plus 28?', 'calculate 3.5 * 2', '-4 / 2'):
                result = asyncio.run(web.review_task(prompt))
                self.assertEqual(result[0], 'simple')
            model.assert_not_called()

    def test_multistep_request_is_still_reviewed(self):
        with patch.object(web, 'LLM') as model:
            model.return_value.ask = AsyncMock(return_value='COMPLEX\nSeveral actions.')
            result = asyncio.run(web.review_task('What is 2 + 2? Then save it to a file.'))
            self.assertEqual(result[0], 'complex')
            model.return_value.ask.assert_awaited_once()

    def test_direct_and_plan_overrides_have_separate_routes(self):
        with patch.object(web, 'review_task', new=AsyncMock(return_value=('simple', 'short task'))) as review:
            direct = asyncio.run(web.choose_execution('task', [], 'direct'))
            self.assertEqual(direct[0], 'direct')
            review.assert_not_awaited()
            planned = asyncio.run(web.choose_execution('task', [], 'plan'))
            self.assertEqual(planned[0], 'planned')
            self.assertIn('short task', planned[1])
            review.assert_awaited_once()

    def test_invalid_execution_mode_is_rejected(self):
        with self.assertRaises(ValidationError):
            web.SessionRequest(prompt='test', execution_mode='unknown')


if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

import asyncio
import unittest

from presence.unfinished import is_probably_unfinished
from tests.helpers import FakeRuntime, make_scheduler


class UnfinishedTests(unittest.IsolatedAsyncioTestCase):
    def test_heuristic_does_not_rewrite_text(self) -> None:
        text = "主要是"
        self.assertTrue(is_probably_unfinished(text))
        self.assertEqual(text, "主要是")
        self.assertFalse(is_probably_unfinished("昨晚只睡了两个小时"))

    async def test_unfinished_message_waits_longer(self) -> None:
        runtime = FakeRuntime()
        scheduler = make_scheduler(runtime)
        try:
            await scheduler.append("s", "主要是")
            await asyncio.sleep(0.04)
            self.assertEqual(runtime.calls, [])
            await scheduler.append("s", "昨晚只睡了两个小时")
            await asyncio.sleep(0.07)
            self.assertEqual(len(runtime.calls), 1)
            self.assertIn("主要是", runtime.calls[0][1])
            self.assertIn("昨晚只睡了两个小时", runtime.calls[0][1])
        finally:
            await scheduler.terminate()

from __future__ import annotations

import asyncio
import unittest

from tests.helpers import FakeRuntime, make_scheduler


class BurstTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_messages_generate_once(self) -> None:
        runtime = FakeRuntime()
        scheduler = make_scheduler(runtime)
        try:
            await scheduler.append("s", "A")
            await asyncio.sleep(0.01)
            await scheduler.append("s", "B")
            await asyncio.sleep(0.01)
            await scheduler.append("s", "C")
            await asyncio.sleep(0.07)
            self.assertEqual(len(runtime.calls), 1)
            prompt = runtime.calls[0][1]
            self.assertIn("A", prompt)
            self.assertIn("B", prompt)
            self.assertIn("C", prompt)
            self.assertEqual(len(runtime.sent), 1)
        finally:
            await scheduler.terminate()

    async def test_no_reply_debt_under_ten_fast_messages(self) -> None:
        runtime = FakeRuntime(generation_delay=0.05)
        scheduler = make_scheduler(runtime)
        try:
            await scheduler.append("s", "initial")
            await asyncio.sleep(0.04)  # first slow generation is now in flight
            for index in range(10):
                await scheduler.append("s", f"M{index}")
                await asyncio.sleep(0.003)
            await asyncio.sleep(0.13)
            self.assertEqual(len(runtime.sent), 1)
            self.assertEqual(runtime.max_concurrent, 1)
            self.assertLessEqual(len(runtime.calls), 2)
            self.assertIn("initial", runtime.calls[-1][1])
            self.assertIn("M0", runtime.calls[-1][1])
            self.assertIn("M9", runtime.calls[-1][1])
        finally:
            await scheduler.terminate()

    async def test_sessions_are_isolated(self) -> None:
        runtime = FakeRuntime()
        scheduler = make_scheduler(runtime)
        try:
            await scheduler.append("A", "from A")
            await scheduler.append("B", "from B")
            await asyncio.sleep(0.07)
            self.assertEqual({session for session, _ in runtime.sent}, {"A", "B"})
            self.assertEqual(scheduler.session_count, 2)
        finally:
            await scheduler.terminate()

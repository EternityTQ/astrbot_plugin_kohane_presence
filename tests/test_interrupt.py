from __future__ import annotations

import asyncio
import unittest

from presence.burst import BurstSnapshot
from presence.scheduler import GeneratedReply
from tests.helpers import FakeRuntime, make_scheduler


class InterruptTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_interrupt_stops_unsent_segments(self) -> None:
        runtime = FakeRuntime()

        async def generate(
            snapshot: BurstSnapshot, prompt: str, _images: list[str]
        ) -> GeneratedReply:
            runtime.calls.append((snapshot.session_id, prompt))
            if snapshot.revision == 1:
                return GeneratedReply(
                    text="",
                    explicit_segments=["segment1", "segment2", "segment3"],
                )
            return GeneratedReply(text="new reply")

        first_sent = asyncio.Event()

        async def send(snapshot: BurstSnapshot, text: str) -> None:
            runtime.sent.append((snapshot.session_id, text))
            if text == "segment1":
                first_sent.set()

        scheduler = make_scheduler(
            runtime,
            max_segments=3,
            segment_delay_min=0.05,
            segment_delay_max=0.05,
        )
        scheduler._generate = generate
        scheduler._send = send
        try:
            await scheduler.append("s", "A")
            await asyncio.wait_for(first_sent.wait(), 0.2)
            await scheduler.append("s", "B")
            await asyncio.sleep(0.13)
            sent_text = [text for _, text in runtime.sent]
            self.assertIn("segment1", sent_text)
            self.assertNotIn("segment2", sent_text)
            self.assertNotIn("segment3", sent_text)
            self.assertIn("new reply", sent_text)
        finally:
            await scheduler.terminate()

    async def test_terminate_cancels_all_background_work(self) -> None:
        runtime = FakeRuntime(generation_delay=1.0)
        scheduler = make_scheduler(runtime)
        await scheduler.append("s", "A")
        await asyncio.sleep(0.04)
        await scheduler.terminate()
        await asyncio.sleep(0)
        self.assertEqual(runtime.sent, [])
        self.assertEqual(scheduler.session_count, 0)

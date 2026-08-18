from __future__ import annotations

import asyncio
import unittest

from presence.burst import BurstSnapshot
from presence.scheduler import GeneratedReply
from tests.helpers import FakeRuntime, make_scheduler


class RevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_generation_is_discarded_then_latest_is_sent(self) -> None:
        runtime = FakeRuntime()
        first_started = asyncio.Event()
        let_first_finish = asyncio.Event()

        async def generate(
            snapshot: BurstSnapshot, prompt: str, _images: list[str]
        ) -> GeneratedReply:
            runtime.calls.append((snapshot.session_id, prompt))
            if snapshot.revision == 1:
                first_started.set()
                try:
                    await let_first_finish.wait()
                except asyncio.CancelledError:
                    # Simulate a provider which cannot actually be cancelled.
                    await let_first_finish.wait()
            return GeneratedReply(text=f"reply:{snapshot.revision}")

        scheduler = make_scheduler(runtime)
        scheduler._generate = generate
        try:
            await scheduler.append("s", "A")
            await asyncio.wait_for(first_started.wait(), 0.2)
            await scheduler.append("s", "B")
            let_first_finish.set()
            await asyncio.sleep(0.11)
            self.assertEqual(runtime.sent, [("s", "reply:2")])
            self.assertIn("A", runtime.calls[-1][1])
            self.assertIn("B", runtime.calls[-1][1])
            self.assertEqual(runtime.commits, ["reply:2"])
        finally:
            await scheduler.terminate()

    async def test_reservation_invalidates_old_reply_before_media_is_ready(self) -> None:
        runtime = FakeRuntime()
        first_started = asyncio.Event()
        let_first_finish = asyncio.Event()

        async def generate(
            snapshot: BurstSnapshot, prompt: str, _images: list[str]
        ) -> GeneratedReply:
            runtime.calls.append((snapshot.session_id, prompt))
            if snapshot.revision == 1:
                first_started.set()
                try:
                    await let_first_finish.wait()
                except asyncio.CancelledError:
                    await let_first_finish.wait()
            return GeneratedReply(text=f"reply:{snapshot.revision}")

        scheduler = make_scheduler(runtime)
        scheduler._generate = generate
        try:
            await scheduler.append("s", "A")
            await asyncio.wait_for(first_started.wait(), 0.2)
            reservation = await scheduler.reserve("s", "[图片]准备中")
            let_first_finish.set()
            await asyncio.sleep(0.05)
            self.assertEqual(runtime.sent, [])
            await scheduler.finalize(reservation)
            await asyncio.sleep(0.07)
            self.assertEqual(runtime.sent, [("s", "reply:2")])
        finally:
            await scheduler.terminate()

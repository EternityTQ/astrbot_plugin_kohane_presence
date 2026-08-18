from __future__ import annotations

import asyncio
import unittest

from presence.media import Attachment
from presence.scheduler import PresenceConfig, PresenceScheduler
from tests.helpers import FakeRuntime


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_caption_completion_does_not_wake_chat(self) -> None:
        runtime = FakeRuntime()

        async def caption(_source: str) -> str:
            await asyncio.sleep(0.02)
            return "困得趴下的表情包"

        scheduler = PresenceScheduler(
            PresenceConfig(
                base_debounce_seconds=0.01,
                unfinished_debounce_seconds=0.02,
                max_burst_age_seconds=0.2,
                image_caption_timeout_seconds=0.04,
            ),
            runtime.generate,
            runtime.send,
            captioner=caption,
        )
        try:
            image = Attachment("image", "x.jpg", use_direct_input=False)
            await scheduler.append("s", "困死了", attachments=[image])
            await asyncio.sleep(0.08)
            self.assertEqual(len(runtime.calls), 1)
            self.assertIn("困得趴下的表情包", runtime.calls[0][1])
        finally:
            await scheduler.terminate()

    async def test_caption_timeout_never_causes_second_generation(self) -> None:
        runtime = FakeRuntime()

        async def caption(_source: str) -> str:
            await asyncio.sleep(0.10)
            return "late caption"

        scheduler = PresenceScheduler(
            PresenceConfig(
                base_debounce_seconds=0.01,
                unfinished_debounce_seconds=0.02,
                max_burst_age_seconds=0.2,
                image_caption_timeout_seconds=0.02,
            ),
            runtime.generate,
            runtime.send,
            captioner=caption,
        )
        try:
            image = Attachment("image", "x.jpg", use_direct_input=False)
            await scheduler.append("s", "困死了", attachments=[image])
            await asyncio.sleep(0.06)
            self.assertEqual(len(runtime.calls), 1)
            self.assertIn("描述暂未就绪", runtime.calls[0][1])
            await asyncio.sleep(0.08)
            self.assertEqual(len(runtime.calls), 1)
        finally:
            await scheduler.terminate()

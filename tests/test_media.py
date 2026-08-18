from __future__ import annotations

import asyncio
import unittest

from presence.media import Attachment, AttachmentStatus
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
            self.assertIn("描述超时", runtime.calls[0][1])
            self.assertEqual(image.status, AttachmentStatus.TIMEOUT)
            self.assertTrue(image.caption_task.cancelled())
            await asyncio.sleep(0.08)
            self.assertEqual(len(runtime.calls), 1)
        finally:
            await scheduler.terminate()

    async def test_provider_ignoring_cancel_cannot_mutate_sealed_burst(self) -> None:
        runtime = FakeRuntime()
        returned = asyncio.Event()

        async def caption(_source: str) -> str:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                returned.set()
                return "too late"

        scheduler = PresenceScheduler(
            PresenceConfig(
                base_debounce_seconds=0.01,
                unfinished_debounce_seconds=0.02,
                max_burst_age_seconds=0.2,
                image_caption_timeout_seconds=0.01,
            ),
            runtime.generate,
            runtime.send,
            captioner=caption,
        )
        try:
            image = Attachment("image", "x.jpg", use_direct_input=False)
            await scheduler.append("s", "看图", attachments=[image])
            await asyncio.wait_for(returned.wait(), 0.2)
            await asyncio.sleep(0.04)
            self.assertEqual(image.status, AttachmentStatus.TIMEOUT)
            self.assertIsNone(image.caption)
            self.assertEqual(len(runtime.calls), 1)
        finally:
            await scheduler.terminate()

    async def test_fast_messages_and_slow_caption_create_no_reply_debt(self) -> None:
        runtime = FakeRuntime()

        async def caption(_source: str) -> str:
            await asyncio.sleep(1)
            return "late"

        scheduler = PresenceScheduler(
            PresenceConfig(
                base_debounce_seconds=0.02,
                unfinished_debounce_seconds=0.03,
                max_burst_age_seconds=0.2,
                image_caption_timeout_seconds=0.01,
            ),
            runtime.generate,
            runtime.send,
            captioner=caption,
        )
        try:
            image = Attachment("image", "x.jpg", use_direct_input=False)
            await scheduler.append("s", "第一条", attachments=[image])
            await scheduler.append("s", "第二条")
            await scheduler.append("s", "第三条")
            await asyncio.sleep(0.10)
            self.assertEqual(len(runtime.calls), 1)
            self.assertEqual(len(runtime.sent), 1)
            self.assertTrue(image.caption_task.cancelled())
        finally:
            await scheduler.terminate()

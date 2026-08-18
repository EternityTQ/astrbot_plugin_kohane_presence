from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from presence.burst import BurstSnapshot
from presence.scheduler import GeneratedReply
from tests.helpers import FakeRuntime, make_scheduler
from presence.sender import SegmentedReplySettings


@dataclass
class Plain:
    text: str


@dataclass
class Image:
    file: str


@dataclass
class Chain:
    chain: list


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

    async def test_user_interrupt_stops_decorated_image_and_stale_after_hook(self) -> None:
        runtime = FakeRuntime()
        first_text_sent = asyncio.Event()
        sent_components: list[str] = []
        after_replies: list[str] = []

        async def generate(
            snapshot: BurstSnapshot, _prompt: str, _images: list[str]
        ) -> GeneratedReply:
            if snapshot.revision == 1:
                return GeneratedReply(
                    text="old",
                    components=[Plain("old"), Image("meme.png")],
                    make_plain=Plain,
                    make_chain=lambda components: Chain(components),
                )
            return GeneratedReply(text="new")

        async def send(_snapshot: BurstSnapshot, unit) -> None:
            if isinstance(unit, Chain):
                name = type(unit.chain[-1]).__name__
                sent_components.append(name)
                if name == "Plain":
                    first_text_sent.set()
            else:
                sent_components.append(str(unit))

        async def after_send(reply: GeneratedReply) -> None:
            after_replies.append(reply.text)

        settings = SegmentedReplySettings(
            enable=True,
            interval=(0.05, 0.05),
            words_count_threshold=80,
            max_segments=2,
        )
        scheduler = make_scheduler(runtime)
        scheduler._generate = generate
        scheduler._send = send
        scheduler._after_send = after_send
        scheduler._segment_settings = lambda _snapshot: settings
        try:
            await scheduler.append("s", "A")
            await asyncio.wait_for(first_text_sent.wait(), 0.2)
            await scheduler.append("s", "B")
            await asyncio.sleep(0.13)
            self.assertEqual(sent_components.count("Image"), 0)
            self.assertIn("new", sent_components)
            self.assertEqual(after_replies, ["new"])
        finally:
            await scheduler.terminate()

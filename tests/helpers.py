from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from presence.burst import BurstSnapshot
from presence.scheduler import GeneratedReply, PresenceConfig, PresenceScheduler


@dataclass
class FakeRuntime:
    calls: list[tuple[str, str]] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    concurrent: int = 0
    max_concurrent: int = 0
    generation_delay: float = 0.0

    async def generate(
        self, snapshot: BurstSnapshot, prompt: str, _images: list[str]
    ) -> GeneratedReply:
        self.calls.append((snapshot.session_id, prompt))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.generation_delay:
                await asyncio.sleep(self.generation_delay)
            return GeneratedReply(text=f"reply:{snapshot.revision}")
        finally:
            self.concurrent -= 1

    async def send(self, snapshot: BurstSnapshot, text: str) -> None:
        self.sent.append((snapshot.session_id, text))

    async def commit(self, _reply: GeneratedReply, text: str) -> None:
        self.commits.append(text)


def make_scheduler(runtime: FakeRuntime, **overrides) -> PresenceScheduler:
    values = {
        "base_debounce_seconds": 0.03,
        "unfinished_debounce_seconds": 0.08,
        "max_burst_age_seconds": 0.2,
        "image_caption_timeout_seconds": 0.02,
        "segment_delay_min": 0.03,
        "segment_delay_max": 0.03,
    }
    values.update(overrides)
    return PresenceScheduler(
        PresenceConfig(**values),
        runtime.generate,
        runtime.send,
        commit=runtime.commit,
    )

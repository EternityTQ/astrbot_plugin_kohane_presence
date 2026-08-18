"""A small revision-aware sender, independent from AstrBot global segmentation."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable

SendSegment = Callable[[str], Awaitable[None]]
IsCurrent = Callable[[], bool]


def split_reply(text: str, max_segments: int) -> list[str]:
    cleaned = text.strip()
    if not cleaned or max_segments <= 1 or len(cleaned) < 80:
        return [cleaned] if cleaned else []
    candidates = [part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()]
    if len(candidates) < 2:
        match = re.search(r"(?<=[。！？!?])\s*", cleaned[len(cleaned) // 3 :])
        if match:
            cut = len(cleaned) // 3 + match.end()
            candidates = [cleaned[:cut].strip(), cleaned[cut:].strip()]
    if len(candidates) <= max_segments:
        return candidates
    return candidates[: max_segments - 1] + ["\n\n".join(candidates[max_segments - 1 :])]


class InterruptibleSender:
    def __init__(self, delay_min: float, delay_max: float) -> None:
        self.delay_min = max(0.0, delay_min)
        self.delay_max = max(self.delay_min, delay_max)

    async def send(
        self,
        segments: list[str],
        send_segment: SendSegment,
        is_current: IsCurrent,
    ) -> list[str]:
        sent: list[str] = []
        for index, segment in enumerate(segments):
            if not is_current():
                break
            if index:
                await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))
                if not is_current():
                    break
            await send_segment(segment)
            sent.append(segment)
        return sent

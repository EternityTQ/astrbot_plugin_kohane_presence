"""Attachment lifecycle for an aggregated user burst."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AttachmentStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


Captioner = Callable[[str], Awaitable[str]]
Cleanup = Callable[[], Any]


@dataclass(slots=True)
class Attachment:
    """An attachment belongs to a user message; completion is never an event."""

    kind: str
    source: str
    use_direct_input: bool = True
    caption: str | None = None
    status: AttachmentStatus = AttachmentStatus.READY
    caption_task: asyncio.Task[None] | None = None
    cleanup: Cleanup | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def start_caption(self, captioner: Captioner) -> asyncio.Task[None]:
        if self.caption_task and not self.caption_task.done():
            return self.caption_task
        self.status = AttachmentStatus.PENDING
        self.caption_task = asyncio.create_task(
            self._caption(captioner),
            name="kpresence-image-caption",
        )
        return self.caption_task

    async def _caption(self, captioner: Captioner) -> None:
        try:
            caption = (await captioner(self.source)).strip()
            if caption:
                self.caption = caption
                self.status = AttachmentStatus.READY
            else:
                self.status = AttachmentStatus.FAILED
        except asyncio.CancelledError:
            raise
        except Exception:
            self.status = AttachmentStatus.FAILED

    def release_when_safe(self) -> None:
        """Release owned media after a late caption finishes, without waking chat."""

        if not self.cleanup:
            return
        cleanup = self.cleanup
        self.cleanup = None
        task = self.caption_task
        if task and not task.done():
            task.add_done_callback(lambda _task: cleanup())
        else:
            cleanup()


async def wait_for_captions(
    attachments: list[Attachment], timeout: float
) -> None:
    """Wait at most *timeout* for current captions; never cancel late captions."""

    tasks = [
        item.caption_task
        for item in attachments
        if item.caption_task is not None and not item.caption_task.done()
    ]
    if tasks and timeout > 0:
        await asyncio.wait(tasks, timeout=timeout)

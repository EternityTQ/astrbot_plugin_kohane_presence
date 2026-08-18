"""Attachment lifecycle for an aggregated user burst."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AttachmentStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    TIMEOUT = "timeout"


Captioner = Callable[[str], Awaitable[str]]
Cleanup = Callable[[], Any]
CaptionGuard = Callable[[], bool]
LifecycleLog = Callable[[str, float], None]


@dataclass(slots=True)
class Attachment:
    """An attachment belongs to a user message; completion is never an event."""

    kind: str
    source: str
    use_direct_input: bool = True
    caption: str | None = None
    status: AttachmentStatus = AttachmentStatus.READY
    caption_task: asyncio.Task[None] | None = None
    captioner: Captioner | None = None
    cleanup: Cleanup | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    caption_revision: int | None = None
    caption_sealed: bool = False
    _accept_caption: CaptionGuard | None = None
    _lifecycle_log: LifecycleLog | None = None
    _caption_started_at: float | None = None

    def start_caption(
        self,
        captioner: Captioner | None = None,
        *,
        accept_caption: CaptionGuard | None = None,
        lifecycle_log: LifecycleLog | None = None,
    ) -> asyncio.Task[None]:
        if self.caption_task and not self.caption_task.done():
            return self.caption_task
        selected_captioner = captioner or self.captioner
        if selected_captioner is None:
            raise RuntimeError("attachment has no captioner")
        self.captioner = selected_captioner
        self._accept_caption = accept_caption
        self._lifecycle_log = lifecycle_log
        self.caption_sealed = False
        self.status = AttachmentStatus.PENDING
        self._caption_started_at = time.monotonic()
        self._log("caption_started")
        self.caption_task = asyncio.create_task(
            self._caption(selected_captioner),
            name="kpresence-image-caption",
        )
        return self.caption_task

    async def _caption(self, captioner: Captioner) -> None:
        try:
            caption = (await captioner(self.source)).strip()
            if not self._can_accept_caption():
                self._log("late_caption_drop")
                return
            if caption:
                self.caption = caption
                self.status = AttachmentStatus.READY
                self._log("caption_ready")
            else:
                self.status = AttachmentStatus.FAILED
        except asyncio.CancelledError:
            self._log("caption_cancelled")
            raise
        except Exception:
            if self._can_accept_caption():
                self.status = AttachmentStatus.FAILED
            else:
                self._log("late_caption_drop")

    def seal_caption(self, *, timed_out: bool = False) -> None:
        """Prevent this attachment's provider callback from mutating the burst."""

        self.caption_sealed = True
        if timed_out and self.status == AttachmentStatus.PENDING:
            self.status = AttachmentStatus.TIMEOUT
            self.metadata["caption_timeout"] = True
            self._log("caption_timeout")
        if self.caption_task and not self.caption_task.done():
            self.caption_task.cancel()

    def _can_accept_caption(self) -> bool:
        if self.caption_sealed:
            return False
        return self._accept_caption is None or self._accept_caption()

    def _log(self, action: str) -> None:
        if self._lifecycle_log:
            started = self._caption_started_at or time.monotonic()
            self._lifecycle_log(action, max(0.0, time.monotonic() - started))

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
    """Wait for this generation, then seal and cancel unfinished providers."""

    pending_items = [
        item
        for item in attachments
        if item.caption_task is not None and not item.caption_task.done()
    ]
    if pending_items and timeout > 0:
        _done, pending = await asyncio.wait(
            [item.caption_task for item in pending_items if item.caption_task],
            timeout=timeout,
        )
    else:
        pending = {
            item.caption_task for item in pending_items if item.caption_task is not None
        }
    for item in attachments:
        if item.caption_task in pending:
            item.seal_caption(timed_out=True)
        else:
            item.caption_sealed = True

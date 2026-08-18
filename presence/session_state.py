"""Per-session mutable state; no module-level scheduler state is used."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .burst import BurstMessage


@dataclass(slots=True)
class SessionState:
    session_id: str
    pending_messages: list[BurstMessage] = field(default_factory=list)
    debounce_task: asyncio.Task[None] | None = None
    generation_task: asyncio.Task[None] | None = None
    generation_revision: int | None = None
    send_task: asyncio.Task[list[str]] | None = None
    last_activity: float = 0.0
    first_pending_at: float | None = None
    revision: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def status(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "revision": self.revision,
            "pending_count": len(self.pending_messages),
            "has_debounce_task": bool(
                self.debounce_task and not self.debounce_task.done()
            ),
            "has_generation_task": bool(
                self.generation_task and not self.generation_task.done()
            ),
            "generation_revision": self.generation_revision,
            "has_send_task": bool(self.send_task and not self.send_task.done()),
        }

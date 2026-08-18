"""Burst/revision state machine.

Messages update the latest session state.  They are not queued reply obligations.
At most one generation per session is effective; every externally visible action
is guarded by a revision check.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .burst import BurstMessage, BurstSnapshot
from .media import Attachment, Captioner, wait_for_captions
from .sender import (
    InterruptibleSender,
    SegmentedReplySettings,
    build_component_units,
    component_delay,
    settings_from_astrbot,
    split_reply,
)
from .session_state import SessionState
from .unfinished import DEFAULT_UNFINISHED_SUFFIXES, is_probably_unfinished


class LoggerLike(Protocol):
    def debug(self, message: str, *args: Any) -> Any: ...
    def error(self, message: str, *args: Any, **kwargs: Any) -> Any: ...
    def warning(self, message: str, *args: Any, **kwargs: Any) -> Any: ...
    def exception(self, message: str, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class PresenceConfig:
    base_debounce_seconds: float = 4.5
    unfinished_debounce_seconds: float = 8.0
    max_burst_age_seconds: float = 25.0
    image_caption_timeout_seconds: float = 2.5
    cancel_stale_generation: bool = True
    cancel_unsent_segments: bool = True
    segmented_reply_enabled: bool = False
    max_segments: int = 2
    segment_delay_min: float = 0.8
    segment_delay_max: float = 2.5
    unfinished_suffixes: tuple[str, ...] = DEFAULT_UNFINISHED_SUFFIXES
    debug: bool = False

    def __post_init__(self) -> None:
        self.base_debounce_seconds = max(0.0, self.base_debounce_seconds)
        self.unfinished_debounce_seconds = max(
            self.base_debounce_seconds, self.unfinished_debounce_seconds
        )
        self.max_burst_age_seconds = max(0.001, self.max_burst_age_seconds)
        self.image_caption_timeout_seconds = max(
            0.0, self.image_caption_timeout_seconds
        )
        self.max_segments = max(1, self.max_segments)


@dataclass(slots=True)
class GeneratedReply:
    text: str
    payload: Any = None
    explicit_segments: list[str] | None = None
    components: list[Any] | None = None
    make_plain: Callable[[str], Any] | None = None
    make_chain: Callable[[list[Any]], Any] | None = None


@dataclass(slots=True)
class MessageReservation:
    state: SessionState
    message: BurstMessage
    revision: int


Generate = Callable[[BurstSnapshot, str, list[str]], Awaitable[GeneratedReply | None]]
Send = Callable[[BurstSnapshot, Any], Awaitable[None]]
Commit = Callable[[GeneratedReply, str], Awaitable[None]]
Discard = Callable[[GeneratedReply], Awaitable[None] | None]
AfterSend = Callable[[GeneratedReply], Awaitable[None]]
SegmentSettings = Callable[[BurstSnapshot], SegmentedReplySettings]


class PresenceScheduler:
    def __init__(
        self,
        config: PresenceConfig,
        generate: Generate,
        send: Send,
        *,
        commit: Commit | None = None,
        discard: Discard | None = None,
        after_send: AfterSend | None = None,
        segment_settings: SegmentSettings | None = None,
        captioner: Captioner | None = None,
        logger: LoggerLike | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._generate = generate
        self._send = send
        self._commit = commit
        self._discard = discard
        self._after_send = after_send
        self._segment_settings = segment_settings
        self._captioner = captioner
        self._logger = logger
        self._clock = clock
        self._states: dict[str, SessionState] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._sequence = 0
        self._closed = False

    @property
    def session_count(self) -> int:
        return len(self._states)

    def status(self) -> list[dict[str, object]]:
        return [state.status() for state in self._states.values()]

    async def append(
        self,
        session_id: str,
        text: str,
        *,
        attachments: list[Attachment] | None = None,
        context: Any = None,
        timestamp: float | None = None,
    ) -> int:
        reservation = await self.reserve(
            session_id,
            text,
            context=context,
            timestamp=timestamp,
        )
        await self.finalize(reservation, attachments=attachments)
        return reservation.revision

    async def reserve(
        self,
        session_id: str,
        text: str,
        *,
        context: Any = None,
        timestamp: float | None = None,
    ) -> MessageReservation:
        """Record arrival and invalidate old work before async media preparation."""

        if self._closed:
            raise RuntimeError("scheduler is closed")
        state = self._states.setdefault(session_id, SessionState(session_id=session_id))
        now = self._clock()
        self._sequence += 1
        message = BurstMessage(
            sequence=self._sequence,
            text=text,
            timestamp=time.time() if timestamp is None else timestamp,
            attachments=[],
            context=context,
            capture_ready=False,
        )
        async with state.lock:
            state.revision += 1
            revision = state.revision
            # Captions belong to the current collecting burst, not to a queued
            # reply obligation. Rebind existing attachments to the new revision.
            for pending in state.pending_messages:
                for attachment in pending.attachments:
                    if not attachment.caption_sealed:
                        attachment.caption_revision = revision
            state.pending_messages.append(message)
            state.last_activity = now
            if state.first_pending_at is None:
                state.first_pending_at = now
            self._debug(state, "burst_append")

            self._cancel_task(state.debounce_task)
            if self.config.cancel_stale_generation:
                self._cancel_task(state.generation_task)
            if self.config.cancel_unsent_segments:
                if state.send_task and not state.send_task.done():
                    self._debug(state, "send_cancelled_by_user")
                self._cancel_task(state.send_task)
            return MessageReservation(state, message, revision)

    async def finalize(
        self,
        reservation: MessageReservation,
        *,
        attachments: list[Attachment] | None = None,
    ) -> None:
        """Finish capture; debounce starts only when every arrived message is ready."""

        state = reservation.state
        async with state.lock:
            if reservation.message.capture_ready:
                return
            reservation.message.attachments.extend(attachments or [])
            reservation.message.capture_ready = True
            if self._closed:
                for attachment in reservation.message.attachments:
                    attachment.seal_caption()
                    attachment.release_when_safe()
                return
            for attachment in reservation.message.attachments:
                attachment.caption_revision = state.revision
                if attachment.caption_task is None and (
                    attachment.kind == "image"
                    and not attachment.use_direct_input
                    and (attachment.captioner is not None or self._captioner is not None)
                ):
                    task = attachment.start_caption(
                        attachment.captioner or self._captioner,
                        accept_caption=lambda item=attachment, current=state: (
                            not self._closed
                            and not item.caption_sealed
                            and item.caption_revision == current.revision
                        ),
                        lifecycle_log=lambda action, duration, item=attachment: self._caption_log(
                            state, item, action, duration
                        ),
                    )
                    self._track(task)
                elif attachment.caption_task is not None:
                    self._track(attachment.caption_task)

            if not all(
                message.capture_ready for message in state.pending_messages
            ):
                return
            now = self._clock()
            latest_text = state.pending_messages[-1].text
            delay, trigger = self._debounce_delay(state, latest_text, now)
            self._cancel_task(state.debounce_task)
            task = asyncio.create_task(
                self._debounce_worker(state, state.revision, delay, trigger),
                name=f"kpresence-debounce:{state.session_id}:{state.revision}",
            )
            state.debounce_task = task
            self._track(task)
            self._debug(state, f"debounce_reset delay={delay:.3f}")

    def _debounce_delay(
        self, state: SessionState, text: str, now: float
    ) -> tuple[float, str]:
        unfinished = is_probably_unfinished(text, self.config.unfinished_suffixes)
        if unfinished:
            self._debug(state, "unfinished_detected")
        desired = (
            self.config.unfinished_debounce_seconds
            if unfinished
            else self.config.base_debounce_seconds
        )
        assert state.first_pending_at is not None
        remaining = self.config.max_burst_age_seconds - (now - state.first_pending_at)
        trigger = "max_burst_age" if remaining <= desired else "burst_timeout"
        return max(0.0, min(desired, remaining)), trigger

    async def _debounce_worker(
        self, state: SessionState, revision: int, delay: float, trigger: str
    ) -> None:
        try:
            await asyncio.sleep(delay)
            async with state.lock:
                if self._closed or revision != state.revision:
                    return
                task = asyncio.create_task(
                    self._generation_worker(state, revision, trigger),
                    name=f"kpresence-generation:{state.session_id}:{revision}",
                )
                state.generation_task = task
                state.generation_revision = revision
                self._track(task)
        except asyncio.CancelledError:
            raise

    async def _generation_worker(
        self, state: SessionState, revision: int, trigger: str
    ) -> None:
        reply: GeneratedReply | None = None
        try:
            async with state.generation_lock:
                async with state.lock:
                    if self._closed or revision != state.revision:
                        return
                    snapshot = BurstSnapshot(
                        session_id=state.session_id,
                        revision=revision,
                        messages=tuple(state.pending_messages),
                    )
                await wait_for_captions(
                    snapshot.attachments,
                    self.config.image_caption_timeout_seconds,
                )
                async with state.lock:
                    if self._closed or revision != state.revision:
                        return
                self._generation_log(snapshot, trigger)
                reply = await self._generate(
                    snapshot,
                    snapshot.format_prompt(),
                    snapshot.direct_image_sources,
                )
                if reply is None:
                    return
                self._debug(state, "generation_finished")

                async with state.lock:
                    if self._closed or revision != state.revision:
                        self._debug(
                            state,
                            f"generation_discarded old={revision} current={state.revision}",
                        )
                        await self._discard_reply(reply)
                        return

                    settings = self._settings(snapshot)
                    units = self._send_units(reply, settings)
                    if not units:
                        await self._discard_reply(reply)
                        return
                    sent_count = 0
                    send_completed = False
                    sent_text = reply.text.strip()
                    max_sequence = snapshot.messages[-1].sequence

                    async def guarded_send(unit: Any) -> None:
                        nonlocal sent_count, send_completed
                        # Make the final revision check and the platform send one
                        # scheduler-critical action. If a user event wins the lock,
                        # stale content cannot cross the boundary afterwards.
                        async with state.lock:
                            if self._closed or state.revision != revision:
                                raise asyncio.CancelledError
                            await self._send(snapshot, unit)
                            sent_count += 1
                            self._debug(
                                state,
                                f"segment_sent {sent_count}/{len(units)}",
                            )
                            if sent_count != len(units):
                                return
                            if self._commit:
                                try:
                                    await self._commit(reply, sent_text)
                                except Exception:
                                    # The platform already received the answer.
                                    # Retrying later would create reply debt.
                                    if self._logger:
                                        self._logger.exception(
                                            "Kohane Presence history commit failed for session=%s",
                                            state.session_id,
                                        )
                            if self._after_send:
                                try:
                                    await self._after_send(reply)
                                    self._debug(
                                        state, "after_send_hooks_completed"
                                    )
                                except Exception:
                                    if self._logger:
                                        self._logger.exception(
                                            "Kohane Presence after-send hooks failed "
                                            "for session=%s",
                                            state.session_id,
                                        )
                            completed = [
                                item
                                for item in state.pending_messages
                                if item.sequence <= max_sequence
                            ]
                            state.pending_messages = [
                                item
                                for item in state.pending_messages
                                if item.sequence > max_sequence
                            ]
                            for item in completed:
                                for attachment in item.attachments:
                                    attachment.release_when_safe()
                            state.first_pending_at = (
                                self._clock() if state.pending_messages else None
                            )
                            send_completed = True

                    send_task = asyncio.create_task(
                        InterruptibleSender(
                            self.config.segment_delay_min,
                            self.config.segment_delay_max,
                        ).send(
                            units,
                            guarded_send,
                            lambda: not self._closed and state.revision == revision,
                            delay_for=lambda unit: component_delay(unit, settings),
                        ),
                        name=f"kpresence-send:{state.session_id}:{revision}",
                    )
                    state.send_task = send_task
                    self._track(send_task)

                sent = await send_task
                if not send_completed or len(sent) != len(units):
                    await self._discard_reply(reply)
                    return
        except asyncio.CancelledError:
            self._debug(
                state,
                f"generation_discarded old={revision} current={state.revision}",
            )
            if reply is not None:
                await self._discard_reply(reply)
            raise
        except Exception:
            if self._logger:
                self._logger.exception(
                    "Kohane Presence background generation failed for session=%s",
                    state.session_id,
                )

    def _settings(self, snapshot: BurstSnapshot) -> SegmentedReplySettings:
        if self._segment_settings:
            return self._segment_settings(snapshot)
        return settings_from_astrbot(
            {},
            fallback_enabled=self.config.segmented_reply_enabled,
            fallback_max_segments=self.config.max_segments,
            fallback_delay_min=self.config.segment_delay_min,
            fallback_delay_max=self.config.segment_delay_max,
        )

    def _send_units(
        self, reply: GeneratedReply, settings: SegmentedReplySettings
    ) -> list[Any]:
        if reply.explicit_segments is not None:
            return [item for item in reply.explicit_segments if item.strip()]
        if (
            reply.components is not None
            and reply.make_plain is not None
            and reply.make_chain is not None
        ):
            return build_component_units(
                reply.components,
                settings,
                make_plain=reply.make_plain,
                make_chain=reply.make_chain,
                logger=self._logger,
            )
        if not settings.enable:
            return [reply.text.strip()] if reply.text.strip() else []
        return split_reply(
            reply.text,
            settings.max_segments,
            settings,
            logger=self._logger,
        )

    async def _discard_reply(self, reply: GeneratedReply) -> None:
        if not self._discard:
            return
        result = self._discard(reply)
        if inspect.isawaitable(result):
            await result

    async def terminate(self) -> None:
        self._closed = True
        for state in self._states.values():
            self._cancel_task(state.debounce_task)
            self._cancel_task(state.generation_task)
            self._cancel_task(state.send_task)
            for message in state.pending_messages:
                for attachment in message.attachments:
                    attachment.seal_caption()
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for state in self._states.values():
            for message in state.pending_messages:
                for attachment in message.attachments:
                    attachment.release_when_safe()
        self._tasks.clear()
        self._states.clear()

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error and self._logger:
            self._logger.error("Kohane Presence task failed: %s", error)

    @staticmethod
    def _cancel_task(task: asyncio.Task[Any] | None) -> None:
        if task and not task.done():
            task.cancel()

    def _debug(self, state: SessionState, action: str) -> None:
        if self.config.debug and self._logger:
            self._logger.debug(
                f"session={state.session_id} revision={state.revision} {action}"
            )

    def _caption_log(
        self,
        state: SessionState,
        attachment: Attachment,
        action: str,
        duration: float,
    ) -> None:
        if not self.config.debug or not self._logger:
            return
        provider = str(attachment.metadata.get("caption_provider_id") or "default")
        self._logger.debug(
            "session=%s revision=%s %s duration=%.3f provider=%s",
            state.session_id,
            state.revision,
            action,
            duration,
            provider,
        )

    def _generation_log(self, snapshot: BurstSnapshot, trigger: str) -> None:
        if not self.config.debug or not self._logger:
            return
        if trigger == "image_caption_ready":
            raise AssertionError("caption completion must not trigger generation")
        event = snapshot.latest_context
        scope = list(getattr(event, "plugins_name", []) or [])
        first = snapshot.messages[0].sequence
        last = snapshot.messages[-1].sequence
        self._logger.debug(
            "generation_started session=%s burst_id=%s-%s revision=%s "
            "trigger=%s plugin_scope=%s",
            snapshot.session_id,
            first,
            last,
            snapshot.revision,
            trigger,
            scope,
        )

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register

from .presence.astrbot_compat import (
    apply_presence_plugin_scope,
    is_registered_command_event,
)
from .presence.llm_bridge import AstrBotLLMBridge
from .presence.media import Attachment
from .presence.scheduler import PresenceConfig, PresenceScheduler
from .presence.sender import settings_from_astrbot


@register(
    "astrbot_plugin_kohane_presence",
    "TQ",
    "面向私聊的 burst 聚合、revision 取消与可打断回复 runtime",
    "0.1.0",
)
class KohanePresencePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.plugin_config = config or {}
        self.bridge = AstrBotLLMBridge(context, logger)
        self.scheduler: PresenceScheduler | None = None
        self.enabled = False
        self.private_enabled = False
        self.allowed_user_ids: set[str] = set()
        self.image_caption_enabled = True
        self.excluded_plugins: list[str] = ["astrbot_plugin_angel_heart"]
        self.inherit_astrbot_segmented_reply = True
        self.debug = False
        self._warned_nonlocal_sessions: set[str] = set()
        self._media_dir = Path(tempfile.gettempdir()) / "astrbot_kohane_presence"

    async def initialize(self) -> None:
        self.enabled = bool(self.plugin_config.get("enabled", True))
        self.private_enabled = bool(self.plugin_config.get("private_enabled", True))
        self.allowed_user_ids = {
            str(item).strip()
            for item in self.plugin_config.get("allowed_user_ids", [])
            if str(item).strip()
        }
        self.image_caption_enabled = bool(
            self.plugin_config.get("image_caption_enabled", True)
        )
        self.debug = bool(self.plugin_config.get("debug", False))
        configured_exclusions = self.plugin_config.get(
            "excluded_plugins", ["astrbot_plugin_angel_heart"]
        )
        if isinstance(configured_exclusions, str):
            configured_exclusions = [configured_exclusions]
        elif not isinstance(configured_exclusions, (list, tuple, set)):
            configured_exclusions = ["astrbot_plugin_angel_heart"]
        self.excluded_plugins = [
            str(item).strip()
            for item in configured_exclusions
            if str(item).strip()
        ]
        self.inherit_astrbot_segmented_reply = bool(
            self.plugin_config.get("inherit_astrbot_segmented_reply", True)
        )

        await self.bridge.initialize()
        if not self.bridge.available:
            logger.error(
                "Kohane Presence disabled: %s", self.bridge.reason_unavailable
            )
            self.enabled = False
            return

        suffixes = self.plugin_config.get("unfinished_suffixes", [])
        config_kwargs: dict[str, Any] = {
            "base_debounce_seconds": float(
                self.plugin_config.get("base_debounce_seconds", 4.5)
            ),
            "unfinished_debounce_seconds": float(
                self.plugin_config.get("unfinished_debounce_seconds", 8.0)
            ),
            "max_burst_age_seconds": float(
                self.plugin_config.get("max_burst_age_seconds", 25.0)
            ),
            "image_caption_timeout_seconds": float(
                self.plugin_config.get("image_caption_timeout_seconds", 2.5)
            ),
            "cancel_stale_generation": bool(
                self.plugin_config.get("cancel_stale_generation", True)
            ),
            "cancel_unsent_segments": bool(
                self.plugin_config.get("cancel_unsent_segments", True)
            ),
            "segmented_reply_enabled": bool(
                self.plugin_config.get("segmented_reply_enabled", False)
            ),
            "max_segments": int(self.plugin_config.get("max_segments", 2)),
            "segment_delay_min": float(
                self.plugin_config.get("segment_delay_min", 0.8)
            ),
            "segment_delay_max": float(
                self.plugin_config.get("segment_delay_max", 2.5)
            ),
            "debug": self.debug,
        }
        if suffixes:
            config_kwargs["unfinished_suffixes"] = tuple(
                str(item).strip() for item in suffixes if str(item).strip()
            )
        self.scheduler = PresenceScheduler(
            PresenceConfig(**config_kwargs),
            self.bridge.generate,
            self._send_segment,
            commit=self.bridge.commit,
            discard=self.bridge.discard,
            after_send=self.bridge.after_send,
            segment_settings=self._segment_settings,
            logger=logger,
        )

        if not self.allowed_user_ids:
            logger.warning(
                "Kohane Presence is enabled but allowed_user_ids is empty; "
                "no private chat will be intercepted."
            )
        logger.info(
            "Kohane Presence initialized for AstrBot 4.27.3; "
            "allowed_users=%d excluded_plugins=%s",
            len(self.allowed_user_ids),
            self.excluded_plugins,
        )

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1000)
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        if not self._should_take_over(event):
            return
        if is_registered_command_event(event, self.on_private_message.__name__):
            return
        assert self.scheduler is not None

        try:
            apply_presence_plugin_scope(
                event,
                self.context,
                self.excluded_plugins,
                logger=logger,
                debug=self.debug,
            )
        except Exception:
            logger.exception("Failed to establish Presence event plugin scope")
            return
        event.should_call_llm(True)
        event.stop_event()
        reservation = None
        try:
            text = event.message_str.strip()
            if not text:
                text = event.get_message_outline().strip()
            reservation = await self.scheduler.reserve(
                event.unified_msg_origin,
                text,
                context=event,
                timestamp=event.created_at,
            )
            attachments = await self._extract_attachments(event)
            await self.scheduler.finalize(reservation, attachments=attachments)
        except Exception:
            logger.exception("Failed to prepare private-message attachments")
            if reservation is not None:
                # Text/outline is still a valid state update. Do not resurrect the
                # already-invalidated old answer because one attachment failed.
                await self.scheduler.finalize(reservation, attachments=[])
            else:
                logger.error("Message reservation failed; restoring pipeline")
                event.should_call_llm(False)
                event.continue_event()

    @filter.command("kpresence_status", priority=100)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kpresence_status(self, event: AstrMessageEvent):
        """显示调度状态，不输出聊天正文。"""

        if self.scheduler is None:
            yield event.plain_result(
                f"Kohane Presence unavailable: {self.bridge.reason_unavailable}"
            )
            return
        states = self.scheduler.status()
        lines = [
            f"enabled={self.enabled and self.private_enabled}",
            f"sessions={len(states)}",
        ]
        for item in states:
            lines.append(
                "session={session_id} revision={revision} pending={pending_count} "
                "debounce={has_debounce_task} generation={has_generation_task} "
                "send={has_send_task}".format(**item)
            )
        yield event.plain_result("\n".join(lines))

    def _should_take_over(self, event: AstrMessageEvent) -> bool:
        eligible = bool(
            self.enabled
            and self.private_enabled
            and self.scheduler is not None
            and self.bridge.available
            and event.is_private_chat()
            and str(event.get_sender_id()) in self.allowed_user_ids
        )
        if not eligible:
            return False
        agent_runner_type = self.context.get_config(
            umo=event.unified_msg_origin
        ).get("provider_settings", {}).get("agent_runner_type", "local")
        if agent_runner_type != "local":
            if event.unified_msg_origin not in self._warned_nonlocal_sessions:
                self._warned_nonlocal_sessions.add(event.unified_msg_origin)
                logger.warning(
                    "Kohane Presence skipped session=%s because agent_runner_type=%s",
                    event.unified_msg_origin,
                    agent_runner_type,
                )
            return False
        return True

    async def _extract_attachments(
        self, event: AstrMessageEvent
    ) -> list[Attachment]:
        images = [item for item in event.get_messages() if isinstance(item, Image)]
        if not images:
            return []
        direct_images = await self.bridge.supports_direct_images(
            event.unified_msg_origin
        )
        attachments: list[Attachment] = []
        try:
            for image in images:
                source = await self._retain_image(image)
                use_direct = direct_images or not self.image_caption_enabled
                attachment = Attachment(
                    kind="image",
                    source=source,
                    use_direct_input=use_direct,
                    cleanup=lambda path=source: self._remove_retained(path),
                )
                if not use_direct:
                    provider_settings = self.context.get_config(
                        umo=event.unified_msg_origin
                    ).get("provider_settings", {})
                    attachment.metadata["caption_provider_id"] = str(
                        provider_settings.get("default_image_caption_provider_id")
                        or "default"
                    )
                    attachment.captioner = (
                        lambda path, umo=event.unified_msg_origin: self.bridge.caption_image(
                            umo, path
                        )
                    )
                attachments.append(attachment)
        except Exception:
            for attachment in attachments:
                if attachment.caption_task and not attachment.caption_task.done():
                    attachment.caption_task.cancel()
                attachment.release_when_safe()
            raise
        return attachments

    async def _retain_image(self, image: Image) -> str:
        source = Path(await image.convert_to_file_path())
        self._media_dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix or ".img"
        target = self._media_dir / f"{uuid.uuid4().hex}{suffix}"
        await asyncio.to_thread(shutil.copy2, source, target)
        return str(target)

    @staticmethod
    def _remove_retained(path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove retained image %s", path, exc_info=True)

    async def _send_segment(self, snapshot, message: Any) -> None:
        event = snapshot.latest_context
        if isinstance(message, str):
            message = MessageChain().message(message)
        await event.send(message)

    def _segment_settings(self, snapshot):
        raw: dict[str, Any] = {}
        if self.inherit_astrbot_segmented_reply:
            raw = (
                self.context.get_config(umo=snapshot.session_id)
                .get("platform_settings", {})
                .get("segmented_reply", {})
            )
        assert self.scheduler is not None
        config = self.scheduler.config
        return settings_from_astrbot(
            raw,
            fallback_enabled=config.segmented_reply_enabled,
            fallback_max_segments=config.max_segments,
            fallback_delay_min=config.segment_delay_min,
            fallback_delay_max=config.segment_delay_max,
        )

    async def terminate(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.terminate()
            self.scheduler = None
        self._warned_nonlocal_sessions.clear()
        logger.info("Kohane Presence terminated")

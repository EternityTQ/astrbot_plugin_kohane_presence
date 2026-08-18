"""Burst data model and the single aggregated user-turn formatter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .media import Attachment, AttachmentStatus


@dataclass(slots=True)
class BurstMessage:
    sequence: int
    text: str
    timestamp: float
    attachments: list[Attachment] = field(default_factory=list)
    context: Any = None
    capture_ready: bool = True


@dataclass(slots=True)
class BurstSnapshot:
    session_id: str
    revision: int
    messages: tuple[BurstMessage, ...]

    @property
    def latest_context(self) -> Any:
        return self.messages[-1].context if self.messages else None

    @property
    def attachments(self) -> list[Attachment]:
        return [attachment for msg in self.messages for attachment in msg.attachments]

    @property
    def direct_image_sources(self) -> list[str]:
        return [
            item.source
            for item in self.attachments
            if item.kind == "image" and item.use_direct_input
        ]

    def format_prompt(self) -> str:
        lines: list[str] = []
        for message in self.messages:
            stamp = datetime.fromtimestamp(message.timestamp).strftime("%H:%M:%S")
            if message.text.strip():
                lines.append(f"[{stamp}] {message.text.strip()}")
            for attachment in message.attachments:
                if attachment.kind != "image":
                    lines.append(f"[{stamp}] [附件：{attachment.kind}]")
                elif attachment.caption:
                    lines.append(f"[{stamp}] [图片：{attachment.caption}]")
                elif attachment.use_direct_input:
                    lines.append(f"[{stamp}] [图片附件]")
                elif attachment.status == AttachmentStatus.FAILED:
                    lines.append(f"[{stamp}] [图片：描述失败]")
                else:
                    lines.append(f"[{stamp}] [图片：描述暂未就绪]")

        body = "\n".join(lines) or "[空消息]"
        return (
            "<recent_user_burst>\n"
            "用户刚刚连续发送了以下消息，请把它们理解为同一次连续表达，"
            "并针对最新整体语境自然接话：\n\n"
            f"{body}\n\n"
            "注意：\n"
            "- 不需要逐条回应\n"
            "- 只回应现在最自然需要接住的部分\n"
            "- 如果前面的信息已经被后面的消息修正，以最新消息为准\n"
            "</recent_user_burst>"
        )

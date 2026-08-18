"""Revision-aware reply splitting and delivery helpers."""

from __future__ import annotations

import asyncio
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SEGMENT_REGEX = r".*?[。？！~…]+|.+$"

SendUnit = Callable[[Any], Awaitable[None]]
IsCurrent = Callable[[], bool]


@dataclass(slots=True)
class SegmentedReplySettings:
    """The AstrBot 4.27.3 segmented_reply fields Presence inherits."""

    enable: bool = False
    split_mode: str = "regex"
    regex: str = DEFAULT_SEGMENT_REGEX
    split_words: list[str] = field(
        default_factory=lambda: ["。", "？", "！", "~", "…"]
    )
    content_cleanup_rule: str = ""
    words_count_threshold: int = 150
    interval_method: str = "random"
    interval: tuple[float, float] = (0.8, 2.5)
    log_base: float = 2.0
    max_segments: int = 2


def settings_from_astrbot(
    raw: Any,
    *,
    fallback_enabled: bool,
    fallback_max_segments: int,
    fallback_delay_min: float,
    fallback_delay_max: float,
) -> SegmentedReplySettings:
    """Normalize a session's AstrBot segmented_reply mapping."""

    if not isinstance(raw, dict):
        raw = {}
    interval = _parse_interval(
        raw.get("interval"), fallback_delay_min, fallback_delay_max
    )
    try:
        threshold = int(raw.get("words_count_threshold", 150))
    except (TypeError, ValueError):
        threshold = 150
    try:
        log_base = float(raw.get("log_base", 2.0))
        if log_base <= 0 or log_base == 1:
            raise ValueError
    except (TypeError, ValueError):
        log_base = 2.0
    split_words = raw.get("split_words", ["。", "？", "！", "~", "…"])
    if not isinstance(split_words, list):
        split_words = ["。", "？", "！", "~", "…"]
    return SegmentedReplySettings(
        enable=bool(raw.get("enable", fallback_enabled)),
        split_mode=str(raw.get("split_mode", "regex") or "regex"),
        regex=str(raw.get("regex", DEFAULT_SEGMENT_REGEX) or DEFAULT_SEGMENT_REGEX),
        split_words=[str(item) for item in split_words if str(item)],
        content_cleanup_rule=str(raw.get("content_cleanup_rule", "") or ""),
        words_count_threshold=threshold,
        interval_method=str(raw.get("interval_method", "random") or "random"),
        interval=interval,
        log_base=log_base,
        max_segments=max(1, int(fallback_max_segments)),
    )


def split_reply(
    text: str,
    max_segments: int,
    settings: SegmentedReplySettings | None = None,
    *,
    logger: Any = None,
) -> list[str]:
    """Split Plain text using AstrBot 4.27.3-compatible semantics.

    AstrBot's ``words_count_threshold`` is an upper bound: text longer than the
    configured value is left intact. It never blocks an explicit regex from
    splitting a short double-newline reply.
    """

    cleaned = text.strip()
    if not cleaned or max_segments <= 1:
        return [cleaned] if cleaned else []
    if settings is None:
        candidates = [
            part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip()
        ]
        if len(candidates) < 2:
            match = re.search(r"(?<=[。！？!?])\s*", cleaned[len(cleaned) // 3 :])
            if match:
                cut = len(cleaned) // 3 + match.end()
                candidates = [cleaned[:cut].strip(), cleaned[cut:].strip()]
    else:
        if len(cleaned) > settings.words_count_threshold:
            return [cleaned]
        if settings.split_mode == "words":
            candidates = _split_by_words(cleaned, settings.split_words)
            candidates = [
                _cleanup_segment(item, settings.content_cleanup_rule, logger)
                for item in candidates
            ]
            candidates = [item for item in candidates if item]
        else:
            try:
                candidates = re.findall(
                    settings.regex, cleaned, re.DOTALL | re.MULTILINE
                )
            except re.error as exc:
                if logger:
                    logger.warning(
                        "Invalid segmented-reply regex; using AstrBot 4.27.3 "
                        "fallback: %s",
                        exc,
                    )
                candidates = re.findall(
                    DEFAULT_SEGMENT_REGEX, cleaned, re.DOTALL | re.MULTILINE
                )
            candidates = [
                _cleanup_segment(item, settings.content_cleanup_rule, logger)
                for item in candidates
                if isinstance(item, str)
            ]
            candidates = [item for item in candidates if item]

    if not candidates:
        return [cleaned]
    if len(candidates) <= max_segments:
        return candidates
    return candidates[: max_segments - 1] + ["\n\n".join(candidates[max_segments - 1 :])]


def component_name(component: Any) -> str:
    return component.__class__.__name__


def is_plain_component(component: Any) -> bool:
    return component_name(component) == "Plain" and isinstance(
        getattr(component, "text", None), str
    )


def build_component_units(
    components: list[Any],
    settings: SegmentedReplySettings,
    *,
    make_plain: Callable[[str], Any],
    make_chain: Callable[[list[Any]], Any],
    logger: Any = None,
) -> list[Any]:
    """Build cancellable MessageChains without dropping non-Plain components."""

    if not settings.enable:
        return [make_chain(list(components))] if components else []

    units: list[Any] = []
    headers: list[Any] = []
    for component in components:
        name = component_name(component)
        if name in {"Reply", "At"}:
            headers.append(component)
            continue
        if is_plain_component(component):
            texts = split_reply(
                component.text,
                settings.max_segments,
                settings,
                logger=logger,
            )
            for text in texts:
                chain = [*headers, make_plain(text)]
                headers.clear()
                units.append(make_chain(chain))
        else:
            chain = [component] if name == "Record" else [*headers, component]
            if name != "Record":
                headers.clear()
            units.append(make_chain(chain))
    return units


class InterruptibleSender:
    def __init__(self, delay_min: float, delay_max: float) -> None:
        self.delay_min = max(0.0, delay_min)
        self.delay_max = max(self.delay_min, delay_max)

    async def send(
        self,
        units: list[Any],
        send_unit: SendUnit,
        is_current: IsCurrent,
        *,
        delay_for: Callable[[Any], float] | None = None,
    ) -> list[Any]:
        sent: list[Any] = []
        for index, unit in enumerate(units):
            if not is_current():
                break
            if index:
                delay = (
                    delay_for(unit)
                    if delay_for
                    else random.uniform(self.delay_min, self.delay_max)
                )
                await asyncio.sleep(max(0.0, delay))
                if not is_current():
                    break
            await send_unit(unit)
            sent.append(unit)
        return sent


def component_delay(unit: Any, settings: SegmentedReplySettings) -> float:
    components = getattr(unit, "chain", None)
    component = unit
    if isinstance(components, list) and components:
        component = next(
            (item for item in components if is_plain_component(item)),
            components[0],
        )
    if settings.interval_method == "log" and is_plain_component(component):
        text = component.text
        count = (
            len(text.split())
            if all(ord(char) < 128 for char in text)
            else len([char for char in text if char.isalnum()])
        )
        value = math.log(count + 1, settings.log_base)
        return random.uniform(value, value + 0.5)
    return random.uniform(*settings.interval)


def _parse_interval(
    value: Any, fallback_min: float, fallback_max: float
) -> tuple[float, float]:
    try:
        if isinstance(value, str):
            parts = [float(item) for item in value.replace(" ", "").split(",")]
        elif isinstance(value, (list, tuple)):
            parts = [float(item) for item in value]
        else:
            raise ValueError
        if len(parts) != 2:
            raise ValueError
        low, high = parts
    except (TypeError, ValueError):
        low, high = float(fallback_min), float(fallback_max)
    low = max(0.0, low)
    return low, max(low, high)


def _split_by_words(text: str, split_words: list[str]) -> list[str]:
    if not split_words:
        return [text]
    escaped = sorted((re.escape(word) for word in split_words), key=len, reverse=True)
    pattern = re.compile(f"(.*?({'|'.join(escaped)})|.+$)", re.DOTALL)
    result: list[str] = []
    for match in pattern.findall(text):
        content = match[0] if isinstance(match, tuple) else match
        for word in split_words:
            if content.endswith(word):
                content = content[: -len(word)]
                break
        if content.strip():
            result.append(content.strip())
    return result or [text]


def _cleanup_segment(text: str, rule: str, logger: Any) -> str:
    if rule:
        try:
            text = re.sub(rule, "", text)
        except re.error as exc:
            if logger:
                logger.warning("Invalid segmented-reply cleanup regex: %s", exc)
    return text.strip()

"""Narrow AstrBot 4.27 compatibility helpers.

These imports are intentionally isolated because AstrBot does not expose a
public API for either "is this an already parsed command?" or for building the
full current-conversation agent without immediately handing its output to the
global responder.
"""

from __future__ import annotations

from typing import Any


def is_registered_command_event(event: Any, own_handler_name: str) -> bool:
    """Use WakingStage's activated handlers instead of guessing from a prefix."""

    try:
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.filter.command_group import CommandGroupFilter

        command_filter_types = (CommandFilter, CommandGroupFilter)
    except ImportError:
        command_filter_types = ()

    handlers = event.get_extra("activated_handlers", []) or []
    for handler in handlers:
        if getattr(handler, "handler_name", "") == own_handler_name:
            continue
        filters = getattr(handler, "event_filters", []) or []
        if command_filter_types and any(
            isinstance(item, command_filter_types) for item in filters
        ):
            return True
        if any(item.__class__.__name__ in {"CommandFilter", "CommandGroupFilter"} for item in filters):
            return True
    return False

"""Narrow AstrBot 4.27 compatibility helpers.

These imports are intentionally isolated because AstrBot does not expose a
public API for either "is this an already parsed command?" or for building the
full current-conversation agent without immediately handing its output to the
global responder.
"""

from __future__ import annotations

from typing import Any


PRESENCE_SCOPE_EXTRA = "kohane_presence_plugin_scope"


def apply_presence_plugin_scope(
    event: Any,
    context: Any,
    excluded_plugins: list[str] | tuple[str, ...] | set[str],
    *,
    logger: Any = None,
    debug: bool = False,
) -> list[str]:
    """Restrict hooks for one Presence-owned event, without global mutation.

    ``None`` and AstrBot's explicit ``["*"]`` both mean "all activated
    plugins".  A pre-existing finite plugin list remains the upper bound.
    Names are compared exactly against ``star.name`` metadata.
    """

    excluded = {
        str(name).strip() for name in excluded_plugins if str(name).strip()
    }
    original = getattr(event, "plugins_name", None)
    uses_all_plugins = original is None or original == ["*"]
    if uses_all_plugins:
        base = [
            str(star.name)
            for star in context.get_all_stars()
            if getattr(star, "activated", False)
            and str(getattr(star, "name", "") or "").strip()
        ]
        base_label = "*"
    else:
        base = [str(name) for name in original if str(name).strip()]
        base_label = repr(base)

    scoped: list[str] = []
    seen: set[str] = set()
    for name in base:
        if name in excluded or name in seen:
            continue
        seen.add(name)
        scoped.append(name)

    event.plugins_name = scoped
    if hasattr(event, "set_extra"):
        event.set_extra(PRESENCE_SCOPE_EXTRA, tuple(scoped))
    if debug and logger:
        logger.debug(
            "plugin_scope base=%s excluded=%s active=%s",
            base_label,
            sorted(excluded),
            scoped,
        )
    return scoped


def has_presence_plugin_scope(event: Any) -> bool:
    if not hasattr(event, "get_extra"):
        return False
    return event.get_extra(PRESENCE_SCOPE_EXTRA) is not None


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

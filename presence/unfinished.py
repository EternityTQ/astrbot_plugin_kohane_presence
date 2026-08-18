"""Small, deterministic heuristics used only to extend the debounce window."""

from __future__ import annotations

DEFAULT_UNFINISHED_SUFFIXES: tuple[str, ...] = (
    "主要是",
    "然后",
    "但是",
    "而且",
    "就是",
    "就是说",
    "我感觉",
    "我觉得",
    "问题是",
    "事情是这样的",
    "还有",
    "不过",
    "等下",
    "不是",
    "说起来",
)


def is_probably_unfinished(
    text: str,
    suffixes: tuple[str, ...] = DEFAULT_UNFINISHED_SUFFIXES,
) -> bool:
    """Return whether *text* strongly suggests that another message will follow.

    This function never rewrites user text.  It only selects a longer debounce.
    """

    normalized = text.strip().rstrip("，,、：:")
    if not normalized:
        return False
    return any(normalized == suffix or normalized.endswith(suffix) for suffix in suffixes)

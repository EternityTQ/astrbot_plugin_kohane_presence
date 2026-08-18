from __future__ import annotations

import unittest

from presence.sender import SegmentedReplySettings, split_reply


class SenderTests(unittest.TestCase):
    def test_short_double_newline_obeys_astrbot_regex(self) -> None:
        settings = SegmentedReplySettings(
            enable=True,
            regex=r"[^\r\n]+(?:\r?\n[^\r\n]+)*",
            words_count_threshold=80,
            max_segments=2,
        )
        text = "快了快了\n\n两小时一眨眼就过了"
        self.assertLess(len(text), 80)
        self.assertEqual(
            split_reply(text, 2, settings),
            ["快了快了", "两小时一眨眼就过了"],
        )

    def test_invalid_regex_uses_astrbot_fallback(self) -> None:
        settings = SegmentedReplySettings(
            enable=True,
            regex="[",
            words_count_threshold=80,
            max_segments=2,
        )
        self.assertEqual(split_reply("第一句。第二句！", 2, settings), ["第一句。", "第二句！"])

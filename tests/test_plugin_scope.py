from __future__ import annotations

import unittest
from dataclasses import dataclass

from presence.astrbot_compat import apply_presence_plugin_scope


@dataclass
class FakeStar:
    name: str
    activated: bool = True


class FakeContext:
    def __init__(self) -> None:
        self.stars = [
            FakeStar("astrbot_plugin_angel_heart"),
            FakeStar("meme_manager"),
            FakeStar("astrbot_plugin_angel_memory"),
            FakeStar("other_plugin"),
            FakeStar("disabled_plugin", activated=False),
        ]

    def get_all_stars(self):
        return self.stars


class FakeEvent:
    def __init__(self, plugins_name=None) -> None:
        self.plugins_name = plugins_name
        self.extras = {}

    def set_extra(self, key, value) -> None:
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class PluginScopeTests(unittest.TestCase):
    def test_takeover_excludes_only_exact_angel_heart_name(self) -> None:
        event = FakeEvent()
        scoped = apply_presence_plugin_scope(
            event,
            FakeContext(),
            ["astrbot_plugin_angel_heart"],
        )
        self.assertNotIn("astrbot_plugin_angel_heart", scoped)
        self.assertIn("meme_manager", scoped)
        self.assertIn("astrbot_plugin_angel_memory", scoped)
        self.assertIn("other_plugin", scoped)
        self.assertNotIn("disabled_plugin", scoped)

    def test_existing_plugin_set_remains_upper_bound(self) -> None:
        event = FakeEvent(
            ["meme_manager", "astrbot_plugin_angel_heart"]
        )
        scoped = apply_presence_plugin_scope(
            event,
            FakeContext(),
            ["astrbot_plugin_angel_heart"],
        )
        self.assertEqual(scoped, ["meme_manager"])

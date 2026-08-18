from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")

    class _Logger:
        def debug(self, *_args, **_kwargs):
            pass

        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

        def exception(self, *_args, **_kwargs):
            pass

    class _Filter:
        EventMessageType = SimpleNamespace(PRIVATE_MESSAGE="private")
        PermissionType = SimpleNamespace(ADMIN="admin")

        @staticmethod
        def event_message_type(*_args, **_kwargs):
            return lambda func: func

        @staticmethod
        def command(*_args, **_kwargs):
            return lambda func: func

        @staticmethod
        def permission_type(*_args, **_kwargs):
            return lambda func: func

    class _Star:
        def __init__(self, context):
            self.context = context

    def register(*_args, **_kwargs):
        return lambda cls: cls

    api.AstrBotConfig = dict
    api.logger = _Logger()
    event.AstrMessageEvent = object
    event.MessageChain = object
    event.filter = _Filter
    components.Image = type("Image", (), {})
    star.Context = object
    star.Star = _Star
    star.register = register

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star,
        }
    )


_install_astrbot_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
KohanePresencePlugin = importlib.import_module(
    "astrbot_plugin_kohane_presence.main"
).KohanePresencePlugin
from astrbot_plugin_kohane_presence.presence.scheduler import (  # noqa: E402
    GeneratedReply,
    PresenceConfig,
    PresenceScheduler,
)


class FakeResult:
    pass


class FakeEvent:
    def __init__(self, *, command: bool = False) -> None:
        self.plugins_name = None
        self.extras = {}
        self.result = None
        self.stopped = False
        self.should_call_llm_values: list[bool] = []
        self.unified_msg_origin = "private:42"
        self.message_str = "hello"
        self.created_at = 1.0
        if command:
            command_filter = type("CommandFilter", (), {})()
            self.extras["activated_handlers"] = [
                SimpleNamespace(
                    handler_name="kpresence_status",
                    event_filters=[command_filter],
                )
            ]

    def is_private_chat(self):
        return True

    def get_sender_id(self):
        return "42"

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def should_call_llm(self, value):
        self.should_call_llm_values.append(value)

    def stop_event(self):
        self.stopped = True
        if self.result is None:
            # AstrBot 4.27.3 creates this empty STOP result as a side effect.
            self.result = FakeResult()

    def clear_result(self):
        self.result = None

    def is_stopped(self):
        return self.stopped

    def get_result(self):
        return self.result

    def get_message_outline(self):
        return self.message_str


class FakeContext:
    def get_all_stars(self):
        return [
            SimpleNamespace(name="astrbot_plugin_angel_heart", activated=True),
            SimpleNamespace(name="meme_manager", activated=True),
            SimpleNamespace(name="astrbot_plugin_angel_memory", activated=True),
        ]

    def get_config(self, *, umo):
        return {"provider_settings": {"agent_runner_type": "local"}}


class FakeScheduler:
    def __init__(self) -> None:
        self.revision = 0
        self.finalized = []

    async def reserve(self, session_id, text, *, context, timestamp):
        self.revision += 1
        return SimpleNamespace(
            revision=self.revision,
            session_id=session_id,
            text=text,
            context=context,
            timestamp=timestamp,
        )

    async def finalize(self, reservation, *, attachments):
        self.finalized.append((reservation, attachments))


def make_plugin() -> tuple[KohanePresencePlugin, FakeScheduler]:
    plugin = object.__new__(KohanePresencePlugin)
    scheduler = FakeScheduler()
    plugin.enabled = True
    plugin.private_enabled = True
    plugin.allowed_user_ids = {"42"}
    plugin.excluded_plugins = ["astrbot_plugin_angel_heart"]
    plugin.context = FakeContext()
    plugin.scheduler = scheduler
    plugin.bridge = SimpleNamespace(available=True)
    plugin.debug = False
    plugin._warned_nonlocal_sessions = set()

    async def no_attachments(_event):
        return []

    plugin._extract_attachments = no_attachments
    return plugin, scheduler


class InterceptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stop_then_clear_keeps_event_stopped_without_result(self) -> None:
        event = FakeEvent()
        event.stop_event()
        event.clear_result()

        self.assertIs(event.is_stopped(), True)
        self.assertIsNone(event.get_result())

    async def test_b_interception_does_not_run_after_send(self) -> None:
        plugin, _scheduler = make_plugin()
        event = FakeEvent()
        after_send_calls = 0

        await plugin.on_private_message(event)
        # RespondStage only reaches OnAfterMessageSentEvent when a result exists.
        if event.get_result() is not None:
            after_send_calls += 1

        self.assertEqual(after_send_calls, 0)
        self.assertIs(event.is_stopped(), True)
        self.assertIsNone(event.get_result())

    async def test_c_complete_presence_send_runs_after_send_once(self) -> None:
        plugin, _scheduler = make_plugin()
        event = FakeEvent()
        calls = {"meme_after": 0}
        sent: list[str] = []
        after_send_completed = asyncio.Event()

        async def generate(_snapshot, _prompt, _images):
            return GeneratedReply("presence reply")

        async def send(_snapshot, unit):
            sent.append(str(unit))

        async def after_send(_reply):
            if "meme_manager" in event.plugins_name:
                calls["meme_after"] += 1
            after_send_completed.set()

        scheduler = PresenceScheduler(
            PresenceConfig(
                base_debounce_seconds=0.01,
                unfinished_debounce_seconds=0.01,
                max_burst_age_seconds=0.05,
            ),
            generate,
            send,
            after_send=after_send,
        )
        plugin.scheduler = scheduler

        try:
            await plugin.on_private_message(event)
            # The original RespondStage still has nothing to send.
            if event.get_result() is not None:
                calls["meme_after"] += 1
            self.assertEqual(calls["meme_after"], 0)

            await asyncio.wait_for(after_send_completed.wait(), 0.5)
            self.assertEqual(sent, ["presence reply"])
            self.assertEqual(calls["meme_after"], 1)
        finally:
            await scheduler.terminate()

    async def test_d_registered_command_is_scoped_without_takeover(self) -> None:
        plugin, scheduler = make_plugin()
        event = FakeEvent(command=True)

        await plugin.on_private_message(event)

        self.assertEqual(scheduler.revision, 0)
        self.assertIs(event.is_stopped(), False)
        self.assertEqual(event.should_call_llm_values, [])
        self.assertNotIn("astrbot_plugin_angel_heart", event.plugins_name)
        self.assertIn("meme_manager", event.plugins_name)
        self.assertIn("astrbot_plugin_angel_memory", event.plugins_name)

    async def test_e_ordinary_message_blocks_all_angel_heart_stages(self) -> None:
        plugin, _scheduler = make_plugin()
        event = FakeEvent()
        calls = {
            "angel_adapter": 0,
            "angel_llm": 0,
            "angel_decorate": 0,
            "angel_after": 0,
        }

        await plugin.on_private_message(event)

        # A later AdapterMessage handler is blocked by stop_event().
        if not event.is_stopped():
            calls["angel_adapter"] += 1
        # Presence's manually driven hooks all obey the narrowed plugin scope.
        for stage in ("angel_llm", "angel_decorate", "angel_after"):
            if "astrbot_plugin_angel_heart" in event.plugins_name:
                calls[stage] += 1

        self.assertEqual(calls, {key: 0 for key in calls})


if __name__ == "__main__":
    unittest.main()

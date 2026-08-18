from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from presence.astrbot_compat import apply_presence_plugin_scope
from presence.llm_bridge import AstrBotLLMBridge, BridgePayload
from presence.scheduler import GeneratedReply


@dataclass
class Plain:
    text: str


@dataclass
class Image:
    file: str


@dataclass
class FakeResult:
    chain: list = field(default_factory=list)
    result_content_type: object = None

    def derive(self, chain):
        return FakeResult(list(chain), self.result_content_type)


class EventType:
    OnLLMRequestEvent = "request"
    OnLLMResponseEvent = "response"
    OnDecoratingResultEvent = "decorate"
    OnAfterMessageSentEvent = "after"


class FakeEvent:
    def __init__(self) -> None:
        self.plugins_name = None
        self.extras = {}
        self.result = None
        self.stopped = False
        self.cleanup_calls = 0

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_result(self, result):
        self.result = result

    def get_result(self):
        return self.result

    def is_stopped(self):
        return self.stopped

    def stop_event(self):
        self.stopped = True

    def cleanup_temporary_local_files(self):
        self.cleanup_calls += 1


@dataclass
class FakeStar:
    name: str
    activated: bool = True


class FakeContext:
    def get_all_stars(self):
        return [
            FakeStar("astrbot_plugin_angel_heart"),
            FakeStar("meme_manager"),
            FakeStar("astrbot_plugin_angel_memory"),
        ]


class HookBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_filters_hooks_and_decoration_preserves_image(self) -> None:
        event = FakeEvent()
        apply_presence_plugin_scope(
            event,
            FakeContext(),
            ["astrbot_plugin_angel_heart"],
        )
        calls = {
            "angel_request": 0,
            "meme_request": 0,
            "angel_decorate": 0,
            "meme_decorate": 0,
            "angel_after": 0,
            "meme_after": 0,
            "response": 0,
        }

        async def call_event_hook(current_event, hook_type, *_args):
            if hook_type == EventType.OnLLMRequestEvent:
                if "astrbot_plugin_angel_heart" in current_event.plugins_name:
                    calls["angel_request"] += 1
                if "meme_manager" in current_event.plugins_name:
                    calls["meme_request"] += 1
            elif hook_type == EventType.OnLLMResponseEvent:
                calls["response"] += 1
            elif hook_type == EventType.OnDecoratingResultEvent:
                if "astrbot_plugin_angel_heart" in current_event.plugins_name:
                    calls["angel_decorate"] += 1
                if "meme_manager" in current_event.plugins_name:
                    calls["meme_decorate"] += 1
                    current_event.get_result().chain.append(Image("meme.png"))
            elif hook_type == EventType.OnAfterMessageSentEvent:
                if "astrbot_plugin_angel_heart" in current_event.plugins_name:
                    calls["angel_after"] += 1
                if "meme_manager" in current_event.plugins_name:
                    calls["meme_after"] += 1
            return current_event.is_stopped()

        bridge = AstrBotLLMBridge(FakeContext(), SimpleNamespace())
        bridge._imports = {
            "Plain": Plain,
            "MessageEventResult": FakeResult,
            "ResultContentType": SimpleNamespace(LLM_RESULT="llm"),
            "EventType": EventType,
            "call_event_hook": call_event_hook,
        }

        await call_event_hook(event, EventType.OnLLMRequestEvent, object())
        response = SimpleNamespace(result_chain=None)
        components, _factory = await bridge._decorate_reply(event, response, "正文")
        payload = BridgePayload(event, None, response, [], None, None)
        await bridge.after_send(GeneratedReply("正文", payload=payload))

        self.assertEqual([type(item).__name__ for item in components], ["Plain", "Image"])
        self.assertEqual(calls["angel_request"], 0)
        self.assertEqual(calls["meme_request"], 1)
        self.assertEqual(calls["angel_decorate"], 0)
        self.assertEqual(calls["meme_decorate"], 1)
        self.assertEqual(calls["angel_after"], 0)
        self.assertEqual(calls["meme_after"], 1)
        self.assertEqual(event.cleanup_calls, 1)
        # Presence bridges only decoration and after-send; the Agent runner owns
        # OnLLMResponse/OnAgentDone and must not receive a duplicate manual call.
        self.assertEqual(calls["response"], 0)

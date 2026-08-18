"""AstrBot 4.27.3 full-agent bridge with delayed history commit."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

from .burst import BurstSnapshot
from .scheduler import GeneratedReply


@dataclass(slots=True)
class BridgePayload:
    event: Any
    request: Any
    response: Any
    messages: list[Any]
    stats: Any
    history_writer: Any


class AstrBotLLMBridge:
    """Run AstrBot's current local Main Agent while withholding its final reply.

    The public plugin SDK can call a provider or a generic tool loop, but cannot
    assemble the selected conversation/persona/KB/tool stack.  This bridge uses
    only that missing internal build path and delays `_save_to_history` until the
    scheduler proves the generation revision is still current.
    """

    def __init__(self, context: Any, logger: Any) -> None:
        self.context = context
        self.logger = logger
        self.available = False
        self.reason_unavailable = "not initialized"
        self._core_version = "unknown"
        self._imports: dict[str, Any] = {}

    async def initialize(self) -> None:
        try:
            from astrbot import __version__
            from astrbot.core.astr_agent_run_util import run_agent
            from astrbot.core.astr_main_agent import (
                _provider_supports_modality,
                build_main_agent,
            )
            from astrbot.core.pipeline.context import call_event_hook
            from astrbot.core.pipeline.process_stage.follow_up import (
                register_active_runner,
                unregister_active_runner,
            )
            from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
                InternalAgentSubStage,
            )
            from astrbot.core.provider.entities import ProviderRequest
            from astrbot.core.star.star_handler import EventType
            from astrbot.core.utils.session_lock import session_lock_manager
        except ImportError as exc:
            self.reason_unavailable = f"AstrBot compatibility import failed: {exc}"
            return

        self._core_version = __version__
        if __version__ != "4.27.3":
            self.reason_unavailable = (
                f"validated for AstrBot 4.27.3, running {__version__}"
            )
            return
        self._imports = {
            "ProviderRequest": ProviderRequest,
            "InternalAgentSubStage": InternalAgentSubStage,
            "build_main_agent": build_main_agent,
            "run_agent": run_agent,
            "call_event_hook": call_event_hook,
            "EventType": EventType,
            "register_active_runner": register_active_runner,
            "unregister_active_runner": unregister_active_runner,
            "session_lock_manager": session_lock_manager,
            "provider_supports_modality": _provider_supports_modality,
        }
        self.available = True
        self.reason_unavailable = ""

    async def supports_direct_images(self, umo: str) -> bool:
        if not self.available:
            return False
        provider = await self.context.get_using_provider_async(umo=umo)
        if provider is None:
            return False
        supports = self._imports["provider_supports_modality"]
        if supports(provider, "image"):
            return True
        settings = self.context.get_config(umo=umo).get("provider_settings", {})
        for provider_id in settings.get("fallback_chat_models", []):
            fallback = self.context.get_provider_by_id(str(provider_id))
            if fallback is not None and supports(fallback, "image"):
                return True
        return False

    async def caption_image(self, umo: str, image_path: str) -> str:
        cfg = self.context.get_config(umo=umo).get("provider_settings", {})
        provider_id = cfg.get("default_image_caption_provider_id") or ""
        if not provider_id:
            raise RuntimeError("no default image caption provider is configured")
        prompt = cfg.get("image_caption_prompt", "Please describe the image in Chinese.")
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            image_urls=[image_path],
        )
        return (response.completion_text or "").strip()

    async def generate(
        self,
        snapshot: BurstSnapshot,
        prompt: str,
        image_urls: list[str],
    ) -> GeneratedReply | None:
        if not self.available:
            raise RuntimeError(self.reason_unavailable)
        event = snapshot.latest_context
        if event is None:
            raise RuntimeError("aggregated burst has no AstrBot event context")

        session_config = self.context.get_config(umo=snapshot.session_id)
        agent_runner_type = session_config["provider_settings"].get(
            "agent_runner_type", "local"
        )
        if agent_runner_type != "local":
            raise RuntimeError(
                "Kohane Presence v0.1 only supports AstrBot's local Agent Runner"
            )

        conversation = await self._get_conversation(event)
        request_type = self._imports["ProviderRequest"]
        request = request_type(
            prompt=prompt,
            image_urls=list(image_urls),
            conversation=conversation,
        )
        event.set_extra("provider_request", request)
        event.set_extra("enable_streaming", False)
        event.continue_event()

        internal = self._imports["InternalAgentSubStage"]()
        shim = SimpleNamespace(
            astrbot_config=session_config,
            plugin_manager=SimpleNamespace(context=self.context),
        )
        await internal.initialize(shim)
        build_config = replace(
            internal.main_agent_cfg,
            streaming_response=False,
            provider_wake_prefix="",
        )
        runner = None
        registered = False
        try:
            lock_manager = self._imports["session_lock_manager"]
            async with lock_manager.acquire_lock(snapshot.session_id):
                build = await self._imports["build_main_agent"](
                    event=event,
                    plugin_context=self.context,
                    config=build_config,
                    apply_reset=False,
                )
                if build is None:
                    return None
                runner = build.agent_runner
                request = build.provider_request

                stopped = await self._imports["call_event_hook"](
                    event,
                    self._imports["EventType"].OnLLMRequestEvent,
                    request,
                )
                if stopped:
                    if build.reset_coro:
                        build.reset_coro.close()
                    return None
                if build.reset_coro:
                    await build.reset_coro

                self._imports["register_active_runner"](snapshot.session_id, runner)
                registered = True
                async for _ in self._imports["run_agent"](
                    runner,
                    internal.max_step,
                    show_tool_use=False,
                    show_tool_call_result=False,
                    stream_to_general=False,
                    show_reasoning=False,
                    buffer_intermediate_messages=True,
                ):
                    pass

                response = runner.get_final_llm_resp()
                if response is None:
                    return None
                text = (response.completion_text or "").strip()
                if not text and response.result_chain:
                    text = response.result_chain.get_plain_text(
                        with_other_comps_mark=True
                    ).strip()
                if not text:
                    return None
                payload = BridgePayload(
                    event=event,
                    request=request,
                    response=response,
                    messages=list(runner.run_context.messages),
                    stats=runner.stats,
                    history_writer=internal,
                )
                return GeneratedReply(text=text, payload=payload)
        except asyncio.CancelledError:
            event.set_extra("agent_stop_requested", True)
            event.stop_event()
            if runner is not None:
                runner.request_stop()
            raise
        finally:
            if registered and runner is not None:
                self._imports["unregister_active_runner"](
                    snapshot.session_id, runner
                )
            # The original pipeline cleaned its files before this delayed run.
            # build_main_agent may have tracked new compressed/quoted media, so
            # this compatibility path must close that second temporary lifecycle.
            try:
                event.cleanup_temporary_local_files()
            except Exception:
                self.logger.warning(
                    "Kohane Presence failed to clean delayed event media",
                    exc_info=True,
                )

    async def commit(self, reply: GeneratedReply, _sent_text: str) -> None:
        payload = reply.payload
        if not isinstance(payload, BridgePayload):
            return
        await payload.history_writer._save_to_history(
            payload.event,
            payload.request,
            payload.response,
            payload.messages,
            payload.stats,
        )

    async def discard(self, reply: GeneratedReply) -> None:
        payload = reply.payload
        if isinstance(payload, BridgePayload):
            payload.event.set_extra("agent_stop_requested", True)
            payload.event.stop_event()

    async def _get_conversation(self, event: Any) -> Any:
        manager = self.context.conversation_manager
        umo = event.unified_msg_origin
        cid = await manager.get_curr_conversation_id(umo)
        if not cid:
            cid = await manager.new_conversation(umo, event.get_platform_id())
        conversation = await manager.get_conversation(umo, cid)
        if conversation is None:
            cid = await manager.new_conversation(umo, event.get_platform_id())
            conversation = await manager.get_conversation(umo, cid)
        if conversation is None:
            raise RuntimeError("unable to create AstrBot conversation")
        return conversation

# AstrBot 4.27.3 API 调研记录

调研对象是本机 `D:\AstrBot\backend\app\astrbot`，其中 `astrbot.__version__ == 4.27.3`。

## 消息入口与默认 LLM 拦截

- 使用 `@filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=1000)` 注册普通私聊 handler；命令已在更早的 WakingStage 完成解析，所以仍可先识别命令再决定是否拦截，同时普通消息不会先落入其他默认优先级 handler。
- `WakingCheckStage` 在 ProcessStage 前已经把通过 filter 的 handler 写入 `event.extra["activated_handlers"]`。插件检查其中是否存在 `CommandFilter` / `CommandGroupFilter`，所以只旁路实际已注册且已解析成功的命令，不按 `/` 前缀猜测。
- 白名单普通私聊调用 `event.should_call_llm(True)` 和 `event.stop_event()`。前者关闭默认 LLM 条件，后者停止后续插件/默认 pipeline；未命中白名单和命令完全不改事件。
- session key 使用 `event.unified_msg_origin`，用户 ID 使用 `event.get_sender_id()`，消息链使用 `event.get_messages()`。

## 当前会话 Agent、人格、工具、知识库

公开的 `Context.llm_generate()` 只调用 provider，`Context.tool_loop_agent()` 也要求调用方自行提供 system prompt、contexts 和 tools；二者不能自动重建当前会话完整能力。

AstrBot 4.27.3 的完整装配位于 `astrbot.core.astr_main_agent.build_main_agent()`。它解析 conversation/persona、人格工具、知识库、workspace、safety prompt 和 fallback provider。因此 `presence/llm_bridge.py` 是一个显式的版本锁定兼容层：

1. 用 conversation manager 获取或创建当前 UMO 的 conversation。
2. 用最终聚合 prompt 和图片构造 `ProviderRequest(conversation=...)`。
3. 调用 `build_main_agent()` 和官方 `run_agent()`，关闭 streaming 且不交给全局 RespondStage。
4. 最终回答只返回 scheduler；scheduler 再做 revision 校验后才发送。

插件启动时严格检查 `4.27.3`，不匹配则不接管消息，避免内部 API 漂移造成吞消息。

## 历史写入

正常 InternalAgentSubStage 会在 Agent 完成后立刻 `_save_to_history()`，早于 RespondStage。那会使稍后被 revision 判 stale 的答案污染长期历史。

兼容桥只保存 `request/response/runner.messages/stats`，不立即写历史。scheduler 确认 revision 未变化且消息成功发送后，才调用 4.27.3 原有 `_save_to_history()`。聚合 prompt 是一个 User turn，最终回答是一个 Assistant turn；被丢弃生成不写 conversation。

## 图片

- `Image.convert_to_file_path()` 的文件可能属于原事件临时生命周期，所以接管时异步复制到插件临时目录，burst 完成后清理。
- 当前聊天 provider 支持 image 时，图片直接随最终聚合请求传入。
- 不支持时，使用 `provider_settings.default_image_caption_provider_id` 对应的 AstrBot provider。caption task 只更新 Attachment，不创建事件。
- scheduler 最多等待插件 caption timeout；晚到 caption 不会再次唤醒聊天。

## 最小私有兼容面

- `presence/astrbot_compat.py`：`CommandFilter`、`CommandGroupFilter`。
- `presence/llm_bridge.py`：`build_main_agent`、`run_agent`、session lock、active runner 注册和原历史写入函数。

4.27.3 没有等价公开 API。其余 burst、revision、media、sender 逻辑不依赖 AstrBot，可独立单元测试。

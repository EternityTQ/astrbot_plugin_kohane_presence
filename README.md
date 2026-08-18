# Kohane Presence

Kohane Presence 是 AstrBot 4.27.3 的白名单私聊 conversation runtime。它把短时间内连续到达的 QQ 消息视为一次表达，只对“当前最新状态”生成一次回答，不维护逐条消息的回复债务。

当前目标环境：Windows、QQ 个人号、NapCat / aiocqhttp、AstrBot 4.27.3、Local Agent Runner。

## 核心行为

```text
用户消息 A ─┐
用户消息 B ─┼─> 同一 UMO 的 BurstBuffer ─> 一次聚合 User turn ─> 一次 Agent
用户消息 C ─┘                                      │
                                                   └─ revision 仍一致才发送和写历史
```

- 每个 `unified_msg_origin` 独立维护 pending burst、debounce、generation、revision 和 send task。
- 新消息重置 debounce，并立即使旧 generation / 未发送 segment 失效。
- `max_burst_age` 到达时仍把当前全部 pending 当成一个整体。
- “主要是 / 然后 / 但是”等词只延长等待，不修改用户文本。
- 图片是 burst 的附件。支持视觉的主模型直接看图；否则使用 AstrBot 已配置的图片描述 provider。caption 完成不会触发第二轮聊天。
- 只有成功发送且 revision 仍为当前值的聚合 turn 才写入 AstrBot conversation history。
- `/help`、`/plugin`、`/sid` 以及其他实际已注册命令继续走 AstrBot 原流程。

## 安装与配置

推荐在 AstrBot WebUI 的插件管理中安装本仓库/压缩包。手动安装时，将整个目录复制为：

```text
%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_kohane_presence
```

源码部署则使用该实例实际的 `<ASTRBOT_ROOT>\data\plugins\astrbot_plugin_kohane_presence`。重启 AstrBot 或在 WebUI 重载插件。

首次配置务必填写白名单；空白名单是安全默认值，不接管任何人：

```json
{
  "enabled": true,
  "private_enabled": true,
  "allowed_user_ids": ["123456789"],
  "base_debounce_seconds": 4.5,
  "unfinished_debounce_seconds": 8.0,
  "max_burst_age_seconds": 25.0,
  "image_caption_enabled": true,
  "image_caption_timeout_seconds": 2.5,
  "cancel_stale_generation": true,
  "cancel_unsent_segments": true,
  "segmented_reply_enabled": false,
  "max_segments": 2,
  "segment_delay_min": 0.8,
  "segment_delay_max": 2.5,
  "debug": true
}
```

若主聊天模型不支持图片，请同时在 AstrBot 配置文件的 Provider 设置中选择“默认图片描述模型”；否则图片会标记为描述失败，但仍不会产生额外聊天轮次。

## 调试

管理员可执行 `/kpresence_status`，查看 session 数、revision、pending 长度以及 debounce/generation/send task 状态；不会输出聊天正文。

`debug=true` 时重点观察：

```text
session=... revision=17 burst_append
session=... revision=17 debounce_reset delay=4.500
session=... revision=17 unfinished_detected
session=... caption_started / caption_ready / caption_failed
session=... revision=17 generation_started revision=17
session=... revision=18 generation_discarded old=17 current=18
session=... revision=18 generation_finished
session=... revision=18 segment_sent index=0
session=... revision=19 send_cancelled_by_user
```

## 人工验收

先保持 `segmented_reply_enabled=false`，按接近真实聊天的节奏发送：

```text
好困哦

我也想眯
但是还在工位
唉唉
[图片]

在听了但还是困困
主要是
昨晚就睡了两个小时
应该说今早
然后昨晚从下午四点睡到九点
我的生物钟完全被摧毁了
[表情包]
```

检查：Bot 不在“主要是”后抢话；一组连续消息只出现一次 `generation_started`；caption 不产生第二次 generation；继续发言后旧 revision 不再发送；数十秒后没有补交旧回复。基础行为稳定后再打开插件分段发送，并在第一段后立即插话，确认后续段停止。

## 与 AngelHeart 共存

> 对启用 Kohane Presence 的私聊会话，应关闭其他插件对同一私聊的“消息排队 / LLM 调度 / 自动回复接管”功能，否则两个 scheduler 可能同时工作。

插件不 monkey patch、不调用 AngelHeart 私有 API。检测到已启用且名称含 `AngelHeart` 的插件时只记录 warning，不擅自修改对方配置。

## 已验证范围与限制

- 已用标准库异步单元测试验证 burst 合并、未完句、caption 正常/超时、stale generation、发送中断、无回复债务、session 隔离和 terminate 清理。
- 已在本机 AstrBot 4.27.3 bundled Python 中验证模块和兼容层导入；尚未代替你完成真实 NapCat、真实 provider 的端到端发信测试。
- v0.1 仅支持 AstrBot Local Agent Runner。Dify/Coze/第三方 Agent Runner 会记录 warning 并跳过接管，让 AstrBot 保持原行为。
- 普通最终回复目前按文本发送；Agent 最终结果中的富媒体组件会退化为文本标记。
- Function Tool 已完整装配，但工具执行期间造成的外部副作用无法靠 revision 回滚；能保证的是最终普通回复和未发送分段不补发。
- 分段发送被中断后，已发第一段不会撤回，整段 Assistant history 不提交；这是避免把未发送内容写进历史的保守选择。
- 兼容桥依赖 4.27.3 内部 API，因此 metadata 和运行时检查都锁定到该补丁版本。升级 AstrBot 前需重新核对 `README-dev.md` 中的兼容面。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试不需要 QQ、NapCat 或真实 LLM。

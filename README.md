# 麦麦留步 (followup_window)

MaiBot 群聊跟进窗口插件：有人 **@ 麦麦**或**引用回复麦麦**后，给这个人开一个短期的跟进窗口。窗口期内，他后续不带 @ 的追问也会被当作「提到了麦麦」直接送进 Planner，由模型自主决定是否回复；群内其他人的闲聊不受影响。

## 解决什么问题

在群聊里，大家 @ 麦麦得到回答后，紧接着的追问往往不会再手动加上 @。但 MaiBot 默认会对不带 @ 的群消息做防抖等待、空闲退避和动态频率门控——刚回复完 @ 消息时额度最紧，追问很容易被门控拦下，看起来像麦麦「装聋作哑」。

如果直接调高整群的发言频率，又会让麦麦频繁插话群友之间的闲聊。**麦麦留步**按对话人开窗，只把当事人的追问当作提及处理。

## 工作方式

- **按人开窗**：群友 @ 麦麦或引用回复麦麦时，为他开启 `window_seconds`（默认 300 秒）的跟进窗口。叫名字、麦麦主动发言或私聊不开启窗口。
- **追问必达 Planner**：窗口期内该群友发的消息（以及任何人不带 @ 引用麦麦的消息）会被标记为「提及麦麦」，跳过退避与频率门控直接进入 Planner；回不回仍由模型决定。
- **自动续期与上限**：
  - 本人再次 @ / 引用麦麦时，续期自己的窗口；
  - 麦麦在群里发言时，为**最近 `window_seconds` 内说过话**的窗口用户续期（已离开对话的人不会被继续续期）；
  - 每人的窗口自开启起最长不超过 `max_active_seconds`（默认 900 秒），群友单纯发消息不会自我续期。
- **即关即净**：窗口状态仅保存在内存中，不改 Prompt、不改宿主发言频率，关闭插件或重启 MaiBot 即清空。

## 安装

> 环境要求：MaiBot 1.3.13 及以上的 1.3.x、`maibot-plugin-sdk` 2.16.x–2.19.x，无额外依赖。

1. **插件市场（推荐）**：在 MaiBot WebUI「插件扩展」中搜索 **麦麦留步**（或 `gsskk.followup-window`）安装。
2. **手动安装**：将本仓库克隆到 MaiBot 的 `plugins/followup_window` 目录：
   ```bash
   git clone https://github.com/gsskk/maibot-plugin-followup /path/to/MaiBot/plugins/followup_window
   ```
3. **启用**：插件默认关闭。安装后在 WebUI 插件配置页（或 `plugins/followup_window/config.toml`）将 `plugin.enabled` 设为 `true`。

## 配置

可在 WebUI 插件配置页直接修改，或编辑 `config.toml`（修改后即时生效，无需重启）：

```toml
[plugin]
enabled = true

[followup]
scope_mode = "all"          # all: 所有群启用 | blacklist: 排除名单中的群 | whitelist: 仅名单中的群启用
groups = []                 # 裸群号列表（如 ["123456789"]），all 模式下忽略
window_seconds = 300        # 单次窗口时长（秒），同时作为麦麦发言续期时「最近说过话」的时间范围
max_active_seconds = 900    # 单次窗口自开启起的最长总时长（秒）

# 可选：按群单独覆盖时长（留 0 或省略则沿用上方默认值）
[[followup.group_overrides]]
group_id = "123456789"
window_seconds = 120
max_active_seconds = 120    # 等于 window_seconds 时表示不续期
```

> 依赖宿主默认开启的 `chat.reply_timing.mentioned_bot_reply = true`；若在宿主配置中关闭该项，被标记的追问将不再强制触发 Planner。

## 部署验证与故障排查

详细的宿主链路说明、运行日志格式对照、验收清单与常见问题排查，请参见 [DEPLOY.md](DEPLOY.md)。

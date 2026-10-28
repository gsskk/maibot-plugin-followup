# 部署指南:把 followup_window 装进 MaiBot

本文档按部署顺序写:前置条件、放置插件、配置、启动、验收。已经部署过、要换新版本的,直接看第 6 节。
插件做什么、各配置项的含义见 `README.md`。

> 本文档中 MaiBot 的接口、路径与配置行为核对自 MaiBot `v1.3.15`–`v1.3.17` 与 `maibot-plugin-sdk` 2.16.0、2.19.0,
> 插件 manifest 声明兼容 MaiBot 1.3.13 及以上的 1.3.x、SDK 2.16.x–2.19.x。

## 1. 它在 MaiBot 里的位置

```
群消息 ──▶ MaiBot Host ──▶ chat.receive.after_process(BLOCKING)──▶ followup_window
                │                                                     │ @ / 引用 bot → 给此人开窗或续期
                │                                                     │ 窗口开着的人的追问、不带 @ 的引用回复
                │                                                     ▼
                │                                  改写这条消息:is_mentioned = true
                ▼
        Maisaka 运行时:按提及处理 ── 防抖缩到 1 秒内、清零空闲退避、跳过动态门控 ──▶ Planner(照常决定回不回)
                       其余消息原样,照常判定(别人的闲聊不受窗口影响)

bot 发言 ──▶ send_service.after_send(OBSERVE)──▶ followup_window:续最近说过话的参与者
```

- 插件只改写入站消息上的「是否提到 bot」标记,不改宿主其他状态,不写库、不写文件;窗口是插件内存态,重启即清空。
- 入站钩子是 BLOCKING:宿主等它返回才继续处理这条消息。绝大多数消息插件不做任何调用即返回;
  唯一的远程调用是判断「引用的是不是 bot」(1.5 秒超时)。整个钩子 3 秒超时,超时或出错时宿主按原消息继续。
- 发送钩子是 OBSERVE,宿主不等它。
- 插件没有额外的 Python 依赖,不需要往 MaiBot 的环境里装任何包。

## 2. 前置条件

| 项目 | 要求 |
|---|---|
| MaiBot | 1.3.13 及以上的 1.3.x,已能在 QQ 群里正常聊天 |
| 提及必回 | `chat.reply_timing.mentioned_bot_reply = true`(默认)。关掉后被标记的追问只会缩短防抖,不再强制进 Planner |
| focus_mode | 关闭。开启时宿主本就逐条处理,插件无意义但无害 |

先确认 MaiBot 本身在测试群里聊顺,再装插件。

## 3. 放置插件

### 3.1 方式一:官方插件市场(推荐)

接入官方插件库后,在 MaiBot WebUI 的「插件扩展」中搜索 **麦麦留步** (或 `gsskk.followup-window`),点击一键安装。

### 3.2 方式二:手动安装

将本仓库克隆或复制到 MaiBot 的 `plugins/followup_window` 目录下(确保 `_manifest.json` 位于 `plugins/followup_window/_manifest.json`):

```bash
# 直接 git clone 到插件目录
git clone https://github.com/gsskk/maibot-plugin-followup /path/to/MaiBot/plugins/followup_window

# 或从本地源码目录复制(注意末尾 /. 会把仓库根目录内容复制进目标目录)
mkdir -p /path/to/MaiBot/plugins/followup_window
cp -r /path/to/maibot-plugin-followup/. /path/to/MaiBot/plugins/followup_window/
```

按 MaiBot 的安装方式,`plugins/` 在:

- **`uv tool install maibot`:** MaiBot 数据目录下的 `plugins/`(与配置文件同级)。
- **源码部署:** 源码目录下的 `plugins/`。
- **Docker:** 宿主机挂载进容器的 `./data/MaiMBot/plugins/`。

## 4. 配置

首次启动 MaiBot 时会在插件目录生成 `config.toml`,默认 `enabled = false`,插件不工作。
推荐在 MaiBot WebUI 的插件配置页填写:每个字段都有中文名称与说明。手写 TOML 时等价于:

```toml
[plugin]
enabled = true
config_version = "0.1.0"    # 由插件维护,不要手改

[followup]
scope_mode = "all"          # all | blacklist | whitelist
groups = []                 # blacklist 模式下关闭的群号,whitelist 模式下开启的群号;all 模式忽略
window_seconds = 300        # 每人的窗口时长;也是 bot 发言时「最近说过话」的判定范围
max_active_seconds = 900    # 每人一次窗口从开窗算最长多久

[[followup.group_overrides]]  # 按群覆盖时长,每个群一段;留 0 或不写的项沿用上面的默认
group_id = "123456789"
window_seconds = 120
max_active_seconds = 120    # 等于 window_seconds 即不续期:窗口只开一次 120 秒
```

几个必须知道的点:

- **默认关闭。** 必须把 `plugin.enabled` 打开,插件才会开窗。
- **群号写裸群号**,不带 `qq:` 之类的平台前缀;加不加引号都行。
- **改配置不用重启。** WebUI 保存与手改 `config.toml` 走宿主同一个入口,立即生效:
  - 关闭插件时所有窗口当场清空;
  - 把某群移出范围(进黑名单,或白名单里去掉)时,该群已开的窗口立即关闭;
  - 改时长只影响之后的开窗与续期,已开的窗口按原来的上限走完。
- **填错不会让插件失效。** 未知的 `scope_mode` 按 `all` 处理;非正数时长回落到默认值;上限短于窗口时按窗口时长算。
  这些问题会以 `[followup] 配置问题:` 的 WARNING 打进日志,启用插件后看一眼。
- **克制靠参与者、时长和名单。** 被标记的追问一律强制进 Planner,与该群 talk_value 高低无关;不希望某群接追问,
  就缩短它的时长或放进黑名单。

## 5. 启动与验收

### 5.1 启动

重启 MaiBot(首次安装需要重启,宿主才会发现新插件目录)。日志里应看到:

```
[followup] 已加载 v<版本号> enabled=True
```

版本号应与 `_manifest.json` 里的 `version` 相同。看不到这一行,见第 7 节常见问题。

### 5.2 验收

在一个启用了的测试群里逐项做,对照插件日志(`[followup]`)与宿主「回复调度 … 动态门控」日志:

| 操作 | 期望 |
|---|---|
| A @ bot 问一句 | 插件日志 `[followup] 开窗(@) 群 … A 截止=… 上限=…`;这条不改写 |
| A 接着不带 @ 追问 | `[followup] 标记提及(追问) 群 … A 消息=<编号>`;宿主日志同一消息编号「检测到提及消息…强制触发」,随后「调度=强制触发」,Planner 一定看到它;回不回由 Planner 决定 |
| bot 刚选择不回、进入「空闲退避」时 A 追问 | 仍被标记并强制触发(退避被清零) |
| 窗口里 C(没 @ 过 bot)和别人闲聊 | 没有 `标记提及` 日志;宿主门控日志是该群原来的频率,照常判定 |
| 引用回复 bot 的某条消息,不带 @ | `[followup] 开窗(引用)`(已开着则是 `续期(引用)`)并紧跟 `标记提及(引用)` |
| B @ bot | `开窗(@) … B`,B 有自己的截止与上限;A 的窗口不变 |
| 只叫 bot 的名字,不 @ | 不开窗,没有 `[followup]` 日志 |
| bot 在群里发言 | `[followup] 续期(发言) 群 … A→… B→…`,只列最近 `window_seconds` 内说过话的人 |
| bot 只和 B 聊、A 不再说话 | A 在最后说话约 `window_seconds` 后不再出现在 `续期(发言)` 里,随后 `到期 … A` |
| bot 一直和某人接话 | 此人的窗口在自己的 `max_active_seconds` 处结束 |
| 黑名单群 / 不在白名单的群里 @ bot | 不开窗 |
| 入站延迟 | 对比宿主「检测到群聊消息」与前后日志的时间戳,开窗的群里不应有可感知的延迟 |

`到期` 日志在该群下一次有消息或 bot 发言时才打出,时间戳会比实际截止晚,看日志里的 `截止=`。

## 6. 更新部署

- **官方插件市场安装:** 在 MaiBot WebUI 插件市场中点击更新。
- **手动安装:**
  - 若使用 git clone: 在 `plugins/followup_window` 下执行 `git pull` 并重启 MaiBot。
  - 若手动复制:
    ```bash
    cp -r /path/to/maibot-plugin-followup/. /path/to/MaiBot/plugins/followup_window/
    # 重启 MaiBot
    ```

检查版本:
```bash
grep '\[followup\] 已加载' /path/to/MaiBot/logs/<当天日志>   # 版本号应与 _manifest.json 的 version 相同
```

插件源码目录里没有 `config.toml`,覆盖不会动现有配置。新版本加了配置项时,WebUI 插件配置页会显示默认值,按需调整。
重启会清掉所有窗口,这是预期行为。

**从 0.2.x 升级:** MaiBot 需为 1.3.13 及以上。0.3.0 不再调发言频率,旧版本可能残留的频率系数在重启 MaiBot 后归 1.0。

## 7. 停用与卸载

- **临时停用:** 在 WebUI 里关闭插件(或把 `plugin.enabled` 改为 `false`)。所有窗口当场清空,不用重启。
- **卸载:** 删掉 `plugins/followup_window/` 目录,重启 MaiBot。插件不改宿主状态,没有需要清理的东西。

## 8. 运维

**日志。** 插件用标准库 logging,logger 名是 `followup_window`,由 MaiBot 收进主日志;所有行都带 `[followup]` 前缀。

| 日志 | 含义 |
|---|---|
| `已加载 v… enabled=…` | 插件启动,版本以 manifest 为准 |
| `配置问题:…` | 配置里有填错的项,按回落规则继续工作 |
| `开窗(@\|引用) 群 … <用户> 截止=… 上限=…` | 为此人开窗 |
| `续期(@\|引用) 群 … <用户> 截止=… 上限=…` | 此人再次 @ / 引用,续自己的期 |
| `续期(发言) 群 … <用户>→<截止> …` | bot 发言,续最近说过话的人 |
| `标记提及(追问\|引用) 群 … <用户> 消息=…` | 这条消息被标成提到了 bot |
| `到期 群 … <用户> 截止=… 累计=…s` | 窗口过期(延迟记录,以 `截止=` 为准) |
| `群 … 移出范围,关闭其中的窗口` | 配置变更后清理 |
| `查被引用消息 … 失败` | 判断引用对象的调用失败或超时,这条按「不是引用 bot」处理 |

**常见问题。**

| 现象 | 原因与处理 |
|---|---|
| 没有 `[followup] 已加载` 这一行 | 目录没放对(应为 `plugins/followup_window/_manifest.json`,注意第 3 节的 `/.`);或宿主日志里有该插件的 manifest / 配置错误(MaiBot 低于 1.3.13 或 SDK 不在 2.16–2.19 时 manifest 校验不通过) |
| 有 `已加载 … enabled=False`,@ 了也不开窗 | 插件没启用,见第 4 节 |
| @ 了不开窗,也没有任何 `[followup]` 日志 | 该群不在范围内(看 `scope_mode` 与 `groups`);或是私聊(私聊不设门控,插件不处理) |
| 有 `标记提及`,宿主却没有强制触发 | 检查 `mentioned_bot_reply` 是否为 true;再看宿主日志是否有该钩子的超时 / 熔断记录 |
| 开了窗,追问没有 `标记提及` | 看追问的人窗口是否还开着(他自己的 `开窗` / `续期` 截止时间);别人 @ 不会续他的期 |
| 窗口里麦麦接了某人跟别人闲聊的话 | 此人窗口开着时,他的每条消息都会让 Planner 看一眼,属已知代价;不想要就缩短该群时长 |
| 窗口结束了,门控比平时还紧一阵 | 强制触发计入门控的一小时统计,属已知限制,一段时间后恢复 |
| 宿主日志出现该钩子超时 | 多半是查被引用消息慢;超时时宿主按原消息继续,不影响收消息。频繁出现请反馈 |

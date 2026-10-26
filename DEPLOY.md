# 部署指南:把 followup_window 装进 MaiBot

本文档按部署顺序写:前置条件、放置插件、配置、启动、验收。已经部署过、要换新版本的,直接看第 6 节。
插件做什么、各配置项的含义见 `README.md`。

> 本文档中 MaiBot 的接口、路径与配置行为核对自 MaiBot `5c7637e` 与 `maibot-plugin-sdk 2.16.0`,
> 插件 manifest 声明兼容 MaiBot 1.3.x、SDK ≥ 2.15.0。

## 1. 它在 MaiBot 里的位置

```
群消息 ──▶ MaiBot Host ──▶ chat.receive.after_process(OBSERVE)──▶ followup_window
                │                                                    │ @ / 引用 bot → 开窗,记下参与者
                │                                                    │ 窗口里参与者的追问、不带 @ 的引用回复
                │                                                    ▼
                │                  frequency.set_adjust(会话, 1/base) ← 有效频率 = 1.0,保持 5 秒
                ▼
         动态门控(dynamic_gate) ── 提频期间放行这一批 ──▶ Planner(照常决定回不回)
                               ── 其余时候照常判定(别人的闲聊不受窗口影响)

bot 发言 ──▶ send_service.after_send(OBSERVE)──▶ followup_window:活跃中则续期
提频 5 秒到 / 窗口到期 / 卸载 / 禁用 / 移出范围 ──▶ frequency.set_adjust(会话, 1.0)
```

- 插件只改每个会话的「发言频率调整系数」,这是 MaiBot 的内存态,重启后一律回到 1.0。插件不写库、不写文件。
- 两个钩子都是 OBSERVE:宿主把它们放到后台跑,不等它们,也不允许它们改写消息,插件出错不会挡住收发消息。
- 插件没有额外的 Python 依赖,不需要往 MaiBot 的环境里装任何包。

## 2. 前置条件

| 项目 | 要求 |
|---|---|
| MaiBot | 1.3.x,已能在 QQ 群里正常聊天 |
| 群聊回复模式 | `reply_trigger_mode = "dynamic"`(MaiBot 群聊默认)。其他模式下没有动态门控,插件无意义但无害 |
| focus_mode | 关闭。开启时宿主有效频率恒为 1.0,插件无意义但无害 |
| 其他插件 | 不要同时启用别的会写「发言频率调整系数」的插件(如按时段调频率、按预算调频率之类),后写的会覆盖先写的 |

先确认 MaiBot 本身在测试群里聊顺,再装插件。

## 3. 放置插件

### 3.1 方式一:官方插件市场(推荐)

接入官方插件库后,在 MaiBot WebUI 的「插件扩展」中搜索 **被 @ 后跟进窗口** (或 `gsskk.followup-window`),点击一键安装。

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
window_seconds = 300        # 被 @ 或 bot 发言后,活跃态再持续这么久
max_active_seconds = 900    # 一次活跃态从开始算最长多久

[[followup.group_overrides]]  # 按群覆盖时长,每个群一段;留 0 或不写的项沿用上面的默认
group_id = "123456789"
window_seconds = 120
max_active_seconds = 120    # 等于 window_seconds 即不续期:窗口只开一次 120 秒
```

几个必须知道的点:

- **默认关闭。** 必须把 `plugin.enabled` 打开,插件才会开窗。
- **群号写裸群号**,不带 `qq:` 之类的平台前缀;加不加引号都行。
- **改配置不用重启。** WebUI 保存与手改 `config.toml` 走宿主同一个入口,立即生效:
  - 关闭插件时宿主会重载插件,所有活跃群的系数当场恢复为 1.0;
  - 把某群移出范围(进黑名单,或白名单里去掉)时,该群已开的窗口立即退出;
  - 改时长只影响之后的开窗与续期,已开的窗口按原来的上限走完。
- **填错不会让插件失效。** 未知的 `scope_mode` 按 `all` 处理;非正数时长回落到默认值;上限短于窗口时按窗口时长算。
  这些问题会以 `[followup] 配置问题:` 的 WARNING 打进日志,启用插件后看一眼。
- **克制靠参与者、时长和名单,不靠降频率。** talk_value 压得很低的群提频时也一样拉到 1.0;不希望那里接追问,
  就缩短它的时长或放进黑名单。理由见 `README.md` 开头。

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
| A @ bot 问一句 | 插件日志 `[followup] 进入(@) 群 … 参与者=A`;这时不提频 |
| A 接着不带 @ 追问 | `[followup] 提频(参与者) … base=… 系数=…`;宿主门控日志里这一批 `频率=1.000`、阈值 0.00、进入 Planner;回不回由 Planner 决定 |
| 约 5 秒后 | `[followup] 提频结束 … →1.0` |
| 窗口里 B(没 @ 过 bot)和别人闲聊 | 没有 `提频` 日志;门控日志是该群原来的频率,照常判定 |
| 引用回复 bot 的某条消息,不带 @ | `[followup] 进入(引用)`(已在活跃态则是 `续期(引用)`)并紧跟 `提频(引用)` |
| B @ 一次 bot 之后再不带 @ 说话 | B 成为参与者,之后的消息出现 `提频(参与者)` |
| 只叫 bot 的名字,不 @ | 不开窗,没有 `[followup]` 日志 |
| bot 在活跃态里发言 | `[followup] 续期(发言) … 截止=…` |
| 之后不再理 bot | 约 `window_seconds` 后 `[followup] 退出(到期) … 参与者=N 累计=…s` |
| bot 一直接话 | 活跃态在 `max_active_seconds` 处结束,即使 bot 还在说 |
| 黑名单群 / 不在白名单的群里 @ bot | 不开窗 |
| 重启 MaiBot | 所有窗口消失,没有残留系数(系数是内存态,重启归 1.0) |

**重启后某群的第一条就是不带 @ 的引用回复**时,可能遇到宿主还没建好该群的会话:插件会在约 4 秒内重试 5 次。
若日志出现 `写入系数未生效(尝试 5 次),放弃这次提频`,这一批按原门控处理,下一条同类消息会再试。

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
重启会清掉所有活跃窗口,这是预期行为。

## 7. 停用与卸载

- **临时停用:** 在 WebUI 里关闭插件(或把 `plugin.enabled` 改为 `false`)。所有活跃群的系数当场恢复为 1.0,不用重启。
- **卸载:** 先停用,再删掉 `plugins/followup_window/` 目录,重启 MaiBot。
  即使直接删目录再重启也安全:系数是内存态,重启后一律回到 1.0。

## 8. 运维

**日志。** 插件用标准库 logging,logger 名是 `followup_window`,由 MaiBot 收进主日志;所有行都带 `[followup]` 前缀。

| 日志 | 含义 |
|---|---|
| `已加载 v… enabled=…` | 插件启动,版本以 manifest 为准 |
| `配置问题:…` | 配置里有填错的项,按回落规则继续工作 |
| `进入(@\|引用) 群 … 参与者=… 截止=… 上限=…` | 开窗 |
| `续期(发言\|@\|引用) 群 … 参与者=N 截止=… 已持续=…s` | 续期 |
| `提频(参与者\|引用) 群 … base=… 系数=… 至=…` | 这一批临时拉到 1.0 |
| `提频结束 群 … 系数=…→1.0` | 5 秒到,恢复 |
| `退出(到期\|卸载\|禁用\|移出范围) 群 … 参与者=N 累计=…s` | 窗口结束;正在提频的已恢复 1.0 |
| `… 发言频率为 0 …,视为静音,不提频` | 该群 talk_value 被设成 0,插件不提频 |
| `写入系数未生效(尝试 5 次),放弃这次提频` | 宿主还没建好该群的会话,这一批按原门控处理 |
| `恢复系数失败` / `恢复系数连续失败 … 放弃` | 恢复 1.0 的调用失败;每秒重试,12 次后放弃。此时该群系数可能仍偏高,直到下一次提频或重启 |

宿主每收到一次系数写入都会立即对待处理的消息跑一次门控,所以「提频」「提频结束」(以及写入重试的每一次)之后
紧跟着会多一条门控判定,这是正常的,不代表来了新消息。

**常见问题。**

| 现象 | 原因与处理 |
|---|---|
| 没有 `[followup] 已加载` 这一行 | 目录没放对(应为 `plugins/followup_window/_manifest.json`,注意第 3 节的 `/.`);或宿主日志里有该插件的 manifest / 配置错误 |
| 有 `已加载 … enabled=False`,@ 了也不开窗 | 插件没启用,见第 4 节 |
| @ 了不开窗,也没有任何 `[followup]` 日志 | 该群不在范围内(看 `scope_mode` 与 `groups`);或是私聊(私聊不设门控,插件不处理) |
| 开了窗,追问还是被门控拦下 | 先看追问的人是不是参与者(开窗的人,或窗口里 @ / 引用过 bot 的人);是的话看有没有 `提频` 日志、门控日志的频率是否为 1.0:不是的话,可能有别的插件也在写频率系数并覆盖了它,或空闲退避把这一批推迟到提频结束之后 |
| 窗口里麦麦还是接了别人闲聊的话 | 若发生在某次 `提频` 后 5 秒内,是同批放行的已知代价;否则那条消息本来就过了原门控,和插件无关 |
| 窗口结束了,门控比平时还紧一阵 | 活跃态里的回复照常计入门控的一小时统计,属已知限制,一段时间后恢复 |
| 活跃态里 bot 反应慢十几秒 | Planner 连续两次「不回复」后的空闲退避在门控之前生效,属已知限制 |
| 想确认某群当前没有残留系数 | 重启 MaiBot 即可清零;或在 WebUI 关闭再打开插件,关闭时会恢复所有活跃群 |

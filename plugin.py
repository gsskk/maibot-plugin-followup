"""麦麦留步 (followup_window) — MaiBot 插件入口。

群聊里 bot 刚被 @ 并回复之后,不带 @ 的追问要过防抖、空闲退避与动态门控才进得了 Planner,常常被搁置。
被 @ 或被引用回复时给这个人开一个跟进窗口;窗口里他的消息(以及不带 @ 的引用回复)被标成「提到了 bot」,
宿主随即按提及处理:防抖缩短、清零空闲退避、跳过门控强制进 Planner,回不回仍由 Planner 决定。别人的消息原样放行。
窗口按人计:自己再 @ / 引用时续期,bot 发言时续最近说过话的人,各有上限。插件不改宿主任何状态,停用无需清理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from .config import FollowupConfig, durations_for, group_enabled, validate
from .detect import at_targets_bot, group_id_of, is_from_bot, quoted_message
from .window import Window, WindowTracker

logger = logging.getLogger("followup_window")

# 入站钩子是阻塞的:宿主等它返回才继续处理这条消息。超时宿主按原消息继续,等于这条没被标记。
ENTRY_TIMEOUT_MS = 3000

# 查被引用消息是钩子里唯一的 RPC;查不到就当作不是引用 bot,不能把入站链路拖到钩子超时。
QUOTE_LOOKUP_TIMEOUT_S = 1.5


def _manifest_version() -> str:
    """启动日志里打印的版本以 manifest 为准,部署后一眼能看出跑的是不是新复制的目录。"""
    try:
        return str(json.loads((Path(__file__).parent / "_manifest.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        return "unknown"


def _dict_get(value: Any, *keys: str) -> Any:
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


class FollowupWindowPlugin(MaiBotPlugin):
    config_model = FollowupConfig

    def __init__(self) -> None:
        super().__init__()
        self._tracker = WindowTracker()
        # 卸载之后为真:正在查被引用消息的钩子回来时不该再开窗。
        self._closed = False

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        logger.info(f"[followup] 已加载 v{_manifest_version()} enabled={self.config.plugin.enabled}")
        if not self.config.plugin.enabled:
            logger.info("[followup] 插件未启用(plugin.enabled=false)")
            return
        self._apply_config()

    async def on_unload(self) -> None:
        self._closed = True
        self._tracker.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del version
        if scope == "self" and isinstance(config_data, dict) and config_data:
            self.set_plugin_config(config_data)
        if self.config.plugin.enabled:
            self._apply_config()
        else:
            self._tracker.clear()

    def _apply_config(self) -> None:
        for problem in validate(self.config):
            logger.warning(f"[followup] 配置问题:{problem}")
        # 改了范围的群立即生效,而不是让已开的窗口拖到上限。
        for session_id in self._tracker.sessions():
            group_id = self._tracker.group_of(session_id) or ""
            if not group_enabled(self.config, group_id) and self._tracker.drop_session(session_id):
                logger.info(f"[followup] 群 {group_id} 移出范围,关闭其中的窗口")

    def _active(self, group_id: str) -> bool:
        return not self._closed and self.config.plugin.enabled and group_enabled(self.config, group_id)

    # ===== 钩子 =====

    @HookHandler(
        "chat.receive.after_process",
        name="followup_mark",
        description="群里有人 @ 或引用回复 bot 后给他开跟进窗口;窗口里他的追问标成提到了 bot",
        mode=HookMode.BLOCKING,
        timeout_ms=ENTRY_TIMEOUT_MS,
    )
    async def on_entry(self, **kwargs: Any) -> dict[str, Any] | None:
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return None
        group_id = group_id_of(message)
        session_id = str(message.get("session_id") or "").strip()
        # 私聊不设门控;通知与 bot 自己的入站回显不是有人在找 bot。
        if not group_id or not session_id or message.get("is_notify") or is_from_bot(message):
            return None
        if not self._active(group_id):
            return None
        sender = str(_dict_get(message, "message_info", "user_info", "user_id") or "").strip()
        if not sender:
            return None
        is_at = at_targets_bot(message)
        is_quote = not is_at and await self._quotes_bot(message, session_id)
        # 无窗口的普通消息直接放过;查被引用消息期间配置可能已变(插件被关或该群已移出范围),也一并拦住。
        if not (is_at or is_quote or self._tracker.has_session(session_id)) or not self._active(group_id):
            return None
        now = time.time()
        self._expire(session_id, group_id, now)
        if is_at or is_quote:
            self._open_or_renew(session_id, group_id, sender, now, "@" if is_at else "引用")
            # 宿主对 @ 本就强制触发;只有不带 @ 的引用回复需要插件标记。
            return self._mark(kwargs, message, group_id, sender, "引用") if is_quote else None
        window = self._tracker.get(session_id, sender, now)
        if window is None:
            return None
        window.last_spoke_at = now
        return self._mark(kwargs, message, group_id, sender, "追问")

    @HookHandler(
        "send_service.after_send",
        name="followup_renew",
        description="bot 在群里成功发言时,续期最近说过话的参与者的跟进窗口(只观察)",
        mode=HookMode.OBSERVE,
    )
    async def on_sent(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        if not kwargs.get("sent") or not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "").strip()
        group_id = self._tracker.group_of(session_id)
        if group_id is None:
            return
        now = time.time()
        self._expire(session_id, group_id, now)
        if not self._tracker.has_session(session_id):
            return
        window_s, _ = durations_for(self.config, group_id)
        renewed = self._tracker.renew_recent(session_id, now, window_s)
        if renewed:
            people = " ".join(f"{w.user_id}→{_fmt_ts(w.deadline)}" for w in renewed)
            logger.info(f"[followup] 续期(发言) 群 {group_id} {people}")

    # ===== 开窗与标记 =====

    async def _quotes_bot(self, message: dict, session_id: str) -> bool:
        quote = quoted_message(message)
        if quote is None:
            return False
        target_id, sender_id = quote
        account = str(message.get("account_id") or "").strip()
        if not account:
            return False
        if sender_id is not None:
            return sender_id == account
        try:
            quoted = await asyncio.wait_for(
                self.ctx.message.get_by_id(target_id, chat_id=session_id), QUOTE_LOOKUP_TIMEOUT_S
            )
        except Exception as e:
            logger.warning(f"[followup] 查被引用消息 {target_id} 失败:{e!r}")
            return False
        # 查不到时 SDK 原样返回宿主的失败包装;拿不到发送者就当作不是引用 bot。
        return isinstance(quoted, dict) and is_from_bot({**quoted, "account_id": account})

    def _expire(self, session_id: str, group_id: str, now: float) -> None:
        for gone in self._tracker.expire(session_id, now):
            logger.info(
                f"[followup] 到期 群 {group_id} {gone.user_id} 截止={_fmt_ts(gone.deadline)} "
                f"累计={gone.deadline - gone.started_at:.0f}s"
            )

    def _open_or_renew(self, session_id: str, group_id: str, sender: str, now: float, reason: str) -> Window:
        window_s, max_s = durations_for(self.config, group_id)
        window, opened = self._tracker.open_or_renew(session_id, group_id, sender, now, window_s, max_s)
        logger.info(
            f"[followup] {'开窗' if opened else '续期'}({reason}) 群 {group_id} {sender} "
            f"截止={_fmt_ts(window.deadline)} 上限={_fmt_ts(window.cap_at)}"
        )
        return window

    def _mark(self, kwargs: dict, message: dict, group_id: str, sender: str, reason: str) -> dict[str, Any] | None:
        if message.get("is_mentioned") is True:
            return None
        logger.info(f"[followup] 标记提及({reason}) 群 {group_id} {sender} 消息={message.get('message_id')}")
        return {"action": "continue", "modified_kwargs": {**kwargs, "message": {**message, "is_mentioned": True}}}


def create_plugin() -> FollowupWindowPlugin:
    return FollowupWindowPlugin()

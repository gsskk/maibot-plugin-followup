"""麦麦留步 (followup_window) — MaiBot 插件入口。

群聊的动态门控在 bot 刚被 @ 并回复之后最紧,不带 @ 的追问反而最难进 Planner。
被 @ 或被引用回复时开一个跟进窗口,记下参与者;窗口里参与者的追问(以及不带 @ 的引用回复)到来时,
把该群有效 talk_value 临时拉到 1.0 约 BOOST_S 秒,让那一批进 Planner,之后恢复,别人的闲聊照常走门控。
窗口只在 bot 发言或再被 @ / 引用时续期,有上限;到期、卸载、禁用、移出范围时正在提频的恢复为 1.0。
插件只按「谁发的、是否 @ / 引用 bot」筛选,不看内容,也不改写消息:钩子全部是 OBSERVE。
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

# 到期与提频结束的检查间隔:提频最多比 BOOST_S 晚这么久结束。
TICK_S = 1

# 一次提频保持多久。宿主在消息进入会话运行时、以及每次写入系数时各跑一次门控,被判「等待」的消息留在待处理里:
# 提频写入时消息已入队,就当场重判放行;还没入队,就在入队那一刻按已提高的频率判定。所以只需盖住
# 「钩子触发 → 消息入队」的间隔(平时不到一秒,冷启动建会话时几秒)。更长只会让别人的消息跟着放行。
BOOST_S = 5

# 入站钩子早于宿主建出会话;会话不存在时 set_adjust 静默无效却仍回成功,所以写完要回读,不一致就等一会儿重写。
# 建会话要等 MCP 初始化与读库,冷启动后可能超过两秒,预算留到四秒。
ADJUST_RETRIES = 5
ADJUST_RETRY_DELAY_S = 1.0

# 恢复 1.0 失败时每拍重试,最多这么多次;一直失败就放弃,下一次提频会按原始值重新算。
RESTORE_RETRIES = 12


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
        # 每个会话一把锁。同一群几乎同时到达两条要提频的消息时,第二次读到的 talk_value 已含第一次的系数:
        # 提频整体持锁,已在提频的只延长。按会话分锁,是为了某个群写入重试的几秒里别的群不必排队。
        self._locks: dict[str, asyncio.Lock] = {}
        self._tick_task: asyncio.Task | None = None
        # 卸载或禁用之后为真。提频可能正在重试(最长约 4 秒),收尾时到期循环已停,它得自己把系数恢复回去。
        self._closed = False

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        logger.info(f"[followup] 已加载 v{_manifest_version()} enabled={self.config.plugin.enabled}")
        if not self.config.plugin.enabled:
            logger.info("[followup] 插件未启用(plugin.enabled=false)")
            return
        await self._apply_config()

    async def on_unload(self) -> None:
        self._closed = True
        await self._stop_tick_loop()
        await self._restore_all("卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del version
        if scope == "self" and isinstance(config_data, dict) and config_data:
            self.set_plugin_config(config_data)
        if self.config.plugin.enabled:
            await self._apply_config()
        else:
            self._closed = True
            await self._stop_tick_loop()
            await self._restore_all("禁用")

    async def _apply_config(self) -> None:
        self._closed = False
        for problem in validate(self.config):
            logger.warning(f"[followup] 配置问题:{problem}")
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop(), name="followup-tick")
        # 改了范围的群立即生效,而不是让已开的窗口拖到上限。
        for window in self._tracker.windows():
            if group_enabled(self.config, window.group_id):
                continue
            async with self._lock_for(window.session_id):
                if self._tracker.pop(window.session_id) is not None:
                    await self._restore_now(window, "移出范围")

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    async def _stop_tick_loop(self) -> None:
        task, self._tick_task = self._tick_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ===== 钩子 =====

    @HookHandler(
        "chat.receive.after_process",
        name="followup_open",
        description="群里有人 @ 或引用回复 bot 后,让参与者接下来的追问进入 Planner(只观察,不改写)",
        mode=HookMode.OBSERVE,
    )
    async def on_entry(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        if not isinstance(message, dict) or not self.config.plugin.enabled:
            return
        group_id = group_id_of(message)
        session_id = str(message.get("session_id") or "").strip()
        # 私聊不设门控;通知与 bot 自己的入站回显不是有人在找 bot。
        if not group_id or not session_id or message.get("is_notify") or is_from_bot(message):
            return
        if not group_enabled(self.config, group_id):
            return
        sender = str(_dict_get(message, "message_info", "user_info", "user_id") or "").strip()
        is_at = at_targets_bot(message)
        is_quote = not is_at and await self._quotes_bot(message, session_id)
        if not is_at and not is_quote and self._tracker.get(session_id) is None:
            return
        async with self._lock_for(session_id):
            # 判定期间(查被引用消息、等锁)配置可能已变:插件被关、或该群已移出范围,拿到锁后再确认一次。
            if self._closed or not self.config.plugin.enabled or not group_enabled(self.config, group_id):
                return
            window = self._tracker.get(session_id)
            if is_at or is_quote:
                window = self._open_or_join(window, session_id, group_id, sender, "@" if is_at else "引用")
                # 宿主对 @ 本就强制触发;只有不带 @ 的引用回复需要提频才进得了 Planner。
                if is_quote:
                    await self._boost(window, "引用")
            elif window is not None and sender in window.participants:
                await self._boost(window, "参与者")

    @HookHandler(
        "send_service.after_send",
        name="followup_renew",
        description="bot 在活跃的群里成功发言时续期跟进窗口(只观察)",
        mode=HookMode.OBSERVE,
    )
    async def on_sent(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        if not kwargs.get("sent") or not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "").strip()
        lock = self._locks.get(session_id)
        if lock is None:
            # 开过窗的会话才有锁;没有锁就不可能活跃,不为每次发送都建一把。
            return
        async with lock:
            window = self._tracker.get(session_id)
            if window is None:
                return
            self._renew(window, "发言")

    # ===== 开窗与续期 =====

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
            quoted = await self.ctx.message.get_by_id(target_id, chat_id=session_id)
        except Exception as e:
            logger.warning(f"[followup] 查被引用消息 {target_id} 失败:{e}")
            return False
        # 查不到时 SDK 原样返回宿主的失败包装;拿不到发送者就当作不是引用 bot。
        return isinstance(quoted, dict) and is_from_bot({**quoted, "account_id": account})

    def _open_or_join(self, window: Window | None, session_id: str, group_id: str, sender: str, reason: str) -> Window:
        if window is not None:
            # 活跃中再被 @ / 引用:发送者成为参与者,续期,不另起上限。
            if sender:
                window.participants.add(sender)
            self._renew(window, reason)
            return window
        window_s, max_s = durations_for(self.config, group_id)
        window = self._tracker.open(session_id, group_id, sender, time.time(), window_s, max_s)
        logger.info(
            f"[followup] 进入({reason}) 群 {group_id} 参与者={sender} "
            f"截止={_fmt_ts(window.deadline)} 上限={_fmt_ts(window.cap_at)}"
        )
        return window

    async def _boost(self, window: Window, reason: str) -> None:
        """把有效频率拉到 1.0 保持 BOOST_S 秒;已在提频的只延长,不重读(读到的 talk_value 已含本插件的系数)。"""
        if window.boost_until is not None:
            window.boost_until = time.time() + BOOST_S
            return
        group_id, session_id = window.group_id, window.session_id
        for attempt in range(ADJUST_RETRIES):
            if attempt:
                await asyncio.sleep(ADJUST_RETRY_DELAY_S)
            # 每次都重读:会话建出来之前宿主匹配不到群专属的 talk_value 规则,第一次读到的可能是全局值。
            try:
                current = float(await self.ctx.frequency.get_current_talk_value(session_id))
                adjust = float(await self.ctx.frequency.get_adjust(session_id))
            except Exception as e:
                logger.warning(f"[followup] 群 {group_id} 读取发言频率失败:{e}")
                continue
            if current <= 0 or adjust <= 0:
                logger.info(f"[followup] 群 {group_id} 发言频率为 {current}(系数 {adjust}),视为静音,不提频")
                return
            base = current / adjust
            if base >= 1.0:
                return
            factor = 1.0 / base
            if not await self._set_adjust_checked(session_id, factor):
                continue
            window.base_talk_value, window.factor = base, factor
            window.boost_until = time.time() + BOOST_S
            if self._closed and self._tracker.pop(session_id) is not None:
                await self._restore_now(window, "提频期间插件已关闭")
                return
            logger.info(
                f"[followup] 提频({reason}) 群 {group_id} base={base:.3f} 系数={factor:.3f} "
                f"至={_fmt_ts(window.boost_until)}"
            )
            return
        logger.warning(f"[followup] 群 {group_id} 写入系数未生效(尝试 {ADJUST_RETRIES} 次),放弃这次提频")

    async def _set_adjust_checked(self, session_id: str, factor: float) -> bool:
        try:
            await self.ctx.frequency.set_adjust(session_id, factor)
            return abs(float(await self.ctx.frequency.get_adjust(session_id)) - factor) <= 1e-6
        except Exception as e:
            logger.warning(f"[followup] 写入系数失败:{e}")
            return False

    def _renew(self, window: Window, reason: str) -> None:
        now = time.time()
        window_s, _ = durations_for(self.config, window.group_id)
        self._tracker.renew(window.session_id, now, window_s)
        logger.info(
            f"[followup] 续期({reason}) 群 {window.group_id} 参与者={len(window.participants)} "
            f"截止={_fmt_ts(window.deadline)} 已持续={now - window.started_at:.0f}s"
        )

    # ===== 退出 =====

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self._tick_once()
            except Exception as e:
                logger.warning(f"[followup] 到期检查失败:{e}")

    async def _tick_once(self) -> None:
        # 恢复的 RPC 也在该会话的锁内:否则参与者此刻发话时会读到「系数已回 1.0、talk_value 还含旧系数」的
        # 半恢复状态,算出偏大的 base,提频不起作用。
        now = time.time()
        due = [
            w.session_id
            for w in self._tracker.windows()
            if w.deadline <= now or (w.boost_until is not None and w.boost_until <= now)
        ]
        for session_id in due:
            async with self._lock_for(session_id):
                window = self._tracker.get(session_id)
                if window is None:
                    continue
                now = time.time()
                if window.deadline <= now:
                    self._tracker.pop(session_id)
                    if await self._restore(window, "到期"):
                        continue
                    self._retry_later(window, keep_window=True)
                elif window.boost_until is not None and window.boost_until <= now:
                    # 等锁期间可能刚被延长,所以拿到锁后再比一次。
                    if await self._unboost(window):
                        continue
                    self._retry_later(window, keep_window=False)

    def _retry_later(self, window: Window, *, keep_window: bool) -> None:
        window.restore_failures += 1
        if window.restore_failures < RESTORE_RETRIES:
            if keep_window:
                self._tracker.put_back(window)
            return
        window.boost_until = None
        window.restore_failures = 0
        logger.warning(
            f"[followup] 群 {window.group_id} 恢复系数连续失败 {RESTORE_RETRIES} 次,放弃;"
            f"系数可能仍为 {window.factor:.3f},直到下一次提频或重启"
        )

    async def _restore_all(self, reason: str) -> None:
        """卸载、禁用时到期循环已停,没有下一拍可等:失败就地重试。"""
        for window in self._tracker.windows():
            async with self._lock_for(window.session_id):
                if self._tracker.pop(window.session_id) is not None:
                    await self._restore_now(window, reason)

    async def _restore_now(self, window: Window, reason: str) -> None:
        """窗口已出表、不再交给到期循环时用:就地重试几次。"""
        for attempt in range(ADJUST_RETRIES):
            if attempt:
                await asyncio.sleep(ADJUST_RETRY_DELAY_S)
            if await self._restore(window, reason):
                return
        logger.warning(f"[followup] 群 {window.group_id} {reason}时恢复系数失败,系数可能仍为 {window.factor:.3f}")

    async def _write_one(self, window: Window) -> bool:
        try:
            ok = bool(await self.ctx.frequency.set_adjust(window.session_id, 1.0))
        except Exception as e:
            logger.warning(f"[followup] 群 {window.group_id} 恢复系数失败:{e}")
            return False
        if not ok:
            logger.warning(f"[followup] 群 {window.group_id} 恢复系数失败:宿主返回失败")
        return ok

    async def _unboost(self, window: Window) -> bool:
        if not await self._write_one(window):
            return False
        window.boost_until = None
        window.restore_failures = 0
        logger.info(f"[followup] 提频结束 群 {window.group_id} 系数={window.factor:.3f}→1.0")
        return True

    async def _restore(self, window: Window, reason: str) -> bool:
        """窗口退出(已出表):正在提频的先恢复 1.0;没在提频的系数本就是 1.0,不必写。"""
        if window.boost_until is not None and not await self._write_one(window):
            return False
        logger.info(
            f"[followup] 退出({reason}) 群 {window.group_id} 参与者={len(window.participants)} "
            f"累计={time.time() - window.started_at:.0f}s"
        )
        return True


def create_plugin() -> FollowupWindowPlugin:
    return FollowupWindowPlugin()

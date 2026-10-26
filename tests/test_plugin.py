import asyncio
import logging
import sys
import time
from types import SimpleNamespace

import pytest

BOT = "10001"
GROUP = "20001"


class _Frequency:
    def __init__(
        self, talk: float = 0.4, ignore_first_sets: int = 0, failing_restores: int = 0, ignore_chats=None
    ) -> None:
        self.talk: dict[str, float] = {}
        self.default_talk = talk
        self.adjust: dict[str, float] = {}
        self.calls: list[tuple[str, float]] = []
        # 宿主在会话还没建出来时 set_adjust 静默无效,但照样回 success。
        self.ignore_first_sets = ignore_first_sets
        self.ignore_chats = ignore_chats  # None = 所有会话都按 ignore_first_sets 落空
        # 恢复 1.0 的 RPC 失败:前 N 次抛错(奇数次)或回 False(偶数次),两种失败形状都要覆盖。
        self.failing_restores = failing_restores

    async def get_current_talk_value(self, chat_id):
        # 值在请求被处理时取定,回到插件之前别的钩子可以插进来(让并发真的交错)。
        value = self.talk.get(chat_id, self.default_talk) * self.adjust.get(chat_id, 1.0)
        await asyncio.sleep(0)
        return value

    async def get_adjust(self, chat_id):
        await asyncio.sleep(0)
        return self.adjust.get(chat_id, 1.0)

    async def set_adjust(self, chat_id, value):
        self.calls.append((chat_id, value))
        await asyncio.sleep(0)  # RPC 往返期间别的钩子可以插进来
        if value == 1.0 and self.failing_restores > 0:
            self.failing_restores -= 1
            if self.failing_restores % 2:
                raise RuntimeError("rpc timeout")
            return False
        if self.ignore_first_sets > 0 and (self.ignore_chats is None or chat_id in self.ignore_chats):
            self.ignore_first_sets -= 1
            return True
        self.adjust[chat_id] = value
        return True


class _Message:
    def __init__(self, by_id=None) -> None:
        self.by_id = by_id or {}
        self.lookups: list[tuple[str, str]] = []

    async def get_by_id(self, message_id, *, chat_id="", **kw):
        self.lookups.append((message_id, chat_id))
        await asyncio.sleep(0)  # RPC 往返期间配置可能被改
        return self.by_id.get(message_id)


class _Ctx:
    def __init__(self, frequency=None, message=None) -> None:
        self.frequency = frequency or _Frequency()
        self.message = message or _Message()
        self.logger = logging.getLogger("test.followup")


def _msg(segments, *, user="111", group=GROUP, session="s1", is_mentioned=False, notify=False):
    return {
        "message_id": "m1",
        "platform": "qq",
        "account_id": BOT,
        "session_id": session,
        "message_info": {
            "user_info": {"user_id": user, "user_nickname": "u", "user_cardname": None},
            "group_info": {"group_id": group, "group_name": "g"} if group is not None else None,
            "additional_config": {},
        },
        "raw_message": segments,
        "is_mentioned": is_mentioned,
        "is_at": False,
        "is_notify": notify,
    }


AT_BOT = [{"type": "at", "data": {"target_user_id": BOT}}, {"type": "text", "data": "在吗"}]


def _quote(sender):
    return [{"type": "reply", "data": {"target_message_id": "30001", "target_message_sender_id": sender}}]


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(fw, monkeypatch):
    c = _Clock()
    # 只替换取当前时刻,日志里的时刻格式化照用真的。
    monkeypatch.setattr(fw, "time", SimpleNamespace(time=c.time, strftime=time.strftime, localtime=time.localtime))
    monkeypatch.setattr(fw, "ADJUST_RETRY_DELAY_S", 0)
    return c


def _make(fw, *, enabled=True, followup=None, frequency=None, message=None):
    p = fw.create_plugin()
    p._ctx = _Ctx(frequency, message)
    p.set_plugin_config({"plugin": {"enabled": enabled, "config_version": "0.1.0"}, "followup": followup or {}})
    return p


@pytest.fixture
async def plugin(fw, clock):
    p = _make(fw)
    yield p
    await p.on_unload()


OPENER = "111"
OTHER = "222"
PLAIN = [{"type": "text", "data": "那这个呢"}]


def plugin_boost_s(p):
    """插件常量 BOOST_S;用 plugin fixture 的测试拿不到 fw,从插件实例所在模块取。"""
    return sys.modules[type(p).__module__].BOOST_S


def _boosts(p, session="s1"):
    return [c for c in p.ctx.frequency.calls if c[0] == session and c[1] != 1.0]


def _restores(p, session="s1"):
    return [c for c in p.ctx.frequency.calls if c == (session, 1.0)]


async def _open_by_at(p, **kw):
    await p.on_entry(message=_msg(AT_BOT, **kw))


# ===== 开窗 =====


async def test_at_opens_window_without_touching_frequency(plugin):
    # 宿主对 @ 本就强制触发;开窗只记下参与者,系数保持 1.0,别人的消息照常走门控。
    await _open_by_at(plugin)
    w = plugin._tracker.get("s1")
    assert w is not None and w.participants == {OPENER} and w.deadline == 1300
    assert plugin.ctx.frequency.calls == []


async def test_quote_without_at_opens_and_boosts(plugin):
    # 引用回复不带 @ 时宿主不会强制触发,这一批要靠插件提频才进得了 Planner。
    await plugin.on_entry(message=_msg(_quote(BOT)))
    w = plugin._tracker.get("s1")
    assert w is not None and w.participants == {OPENER}
    assert plugin.ctx.frequency.calls == [("s1", 2.5)]
    assert w.boost_until == 1000 + plugin_boost_s(plugin)
    assert plugin.ctx.message.lookups == []


async def test_quote_resolved_via_get_by_id_opens(fw, clock):
    quoted = _msg([{"type": "text", "data": "上一句"}], user=BOT)
    p = _make(fw, message=_Message({"30001": quoted}))
    await p.on_entry(message=_msg(_quote(None)))
    assert p.ctx.message.lookups == [("30001", "s1")]
    assert p._tracker.get("s1") is not None and _boosts(p) == [("s1", 2.5)]
    await p.on_unload()


async def test_quote_to_other_user_does_not_open(fw, clock):
    other = _msg([{"type": "text", "data": "x"}], user="10002")
    p = _make(fw, message=_Message({"30001": other}))
    await p.on_entry(message=_msg(_quote("10002")))
    await p.on_entry(message=_msg(_quote(None)))
    assert p._tracker.get("s1") is None and p.ctx.frequency.calls == []
    await p.on_unload()


async def test_quote_lookup_failure_does_not_open(fw, clock):
    p = _make(fw, message=_Message({"30001": {"success": False, "error": "not found"}}))
    await p.on_entry(message=_msg(_quote(None)))
    assert p._tracker.get("s1") is None
    await p.on_unload()


async def test_name_mention_does_not_open(plugin):
    await plugin.on_entry(message=_msg([{"type": "text", "data": "麦麦在吗"}], is_mentioned=True))
    assert plugin._tracker.get("s1") is None and plugin.ctx.frequency.calls == []


async def test_plain_message_without_window_ignored(plugin):
    await plugin.on_entry(message=_msg(PLAIN))
    assert plugin._tracker.get("s1") is None and plugin.ctx.frequency.calls == []


# ===== 窗口里只认参与者 =====


async def test_participant_followup_boosts_then_drops(plugin, clock):
    await _open_by_at(plugin)
    await plugin.on_entry(message=_msg(PLAIN))
    w = plugin._tracker.get("s1")
    assert plugin.ctx.frequency.calls == [("s1", 2.5)]
    assert (w.base_talk_value, w.factor, w.boost_until) == (0.4, 2.5, 1000 + plugin_boost_s(plugin))
    clock.now = w.boost_until - 1
    await plugin._tick_once()
    assert plugin.ctx.frequency.adjust["s1"] == 2.5
    clock.now = w.boost_until
    await plugin._tick_once()
    # 提频结束,窗口还在:参与者下一条消息还会再提频。
    assert plugin.ctx.frequency.adjust["s1"] == 1.0 and w.boost_until is None
    assert plugin._tracker.get("s1") is w


async def test_non_participant_message_is_not_boosted(plugin):
    await _open_by_at(plugin)
    await plugin.on_entry(message=_msg(PLAIN, user=OTHER))
    assert plugin.ctx.frequency.calls == []


async def test_at_in_window_adds_participant_and_renews(plugin, clock):
    await _open_by_at(plugin)
    clock.now = 1100
    await _open_by_at(plugin, user=OTHER)
    w = plugin._tracker.get("s1")
    assert w.participants == {OPENER, OTHER} and w.deadline == 1400
    assert plugin.ctx.frequency.calls == []
    await plugin.on_entry(message=_msg(PLAIN, user=OTHER))
    assert plugin.ctx.frequency.calls == [("s1", 2.5)]


async def test_quote_in_window_adds_participant_and_boosts(plugin):
    await _open_by_at(plugin)
    await plugin.on_entry(message=_msg(_quote(BOT), user=OTHER))
    assert plugin._tracker.get("s1").participants == {OPENER, OTHER}
    assert plugin.ctx.frequency.calls == [("s1", 2.5)]


async def test_at_while_active_renews_without_new_cap(plugin, clock):
    await _open_by_at(plugin)
    clock.now = 1800
    await _open_by_at(plugin)
    w = plugin._tracker.get("s1")
    assert (w.deadline, w.cap_at) == (1900, 1900)


async def test_boost_extends_without_rewrite(plugin, clock):
    await _open_by_at(plugin)
    await plugin.on_entry(message=_msg(PLAIN))
    clock.now = 1005
    await plugin.on_entry(message=_msg(PLAIN))
    assert plugin.ctx.frequency.calls == [("s1", 2.5)]
    assert plugin._tracker.get("s1").boost_until == 1005 + plugin_boost_s(plugin)


async def test_muted_group_is_not_boosted(fw, clock):
    p = _make(fw, frequency=_Frequency(talk=0.0))
    await _open_by_at(p)
    await p.on_entry(message=_msg(PLAIN))
    assert p.ctx.frequency.calls == [] and p._tracker.get("s1").boost_until is None
    await p.on_unload()


async def test_full_talk_value_needs_no_boost(fw, clock):
    p = _make(fw, frequency=_Frequency(talk=1.0))
    await _open_by_at(p)
    await p.on_entry(message=_msg(PLAIN))
    assert p.ctx.frequency.calls == [] and p._tracker.get("s1").boost_until is None
    await p.on_unload()


async def test_concurrent_boosts_write_once(plugin):
    await _open_by_at(plugin)
    await asyncio.gather(plugin.on_entry(message=_msg(PLAIN)), plugin.on_entry(message=_msg(PLAIN)))
    assert plugin.ctx.frequency.calls == [("s1", 2.5)]
    assert plugin._tracker.get("s1").base_talk_value == 0.4


async def test_boost_end_racing_participant_message(plugin, clock):
    await _open_by_at(plugin)
    await plugin.on_entry(message=_msg(PLAIN))
    clock.now = 1000 + plugin_boost_s(plugin)
    # 提频结束的恢复还没回来时参与者又发话:新提频要按原始 talk_value 算,而不是读到半恢复的状态。
    await asyncio.gather(plugin._tick_once(), plugin.on_entry(message=_msg(PLAIN)))
    w = plugin._tracker.get("s1")
    assert w.boost_until is not None and w.factor == 2.5
    assert plugin.ctx.frequency.adjust["s1"] == 2.5


# ===== 写入失败与冷启动 =====


async def test_boost_retries_until_adjust_sticks(fw, clock):
    p = _make(fw, frequency=_Frequency(ignore_first_sets=2))
    await p.on_entry(message=_msg(_quote(BOT)))
    assert p.ctx.frequency.calls == [("s1", 2.5)] * 3
    assert p._tracker.get("s1").boost_until is not None
    await p.on_unload()


async def test_boost_retry_budget_covers_slow_session_creation(fw, clock):
    # 重启后某群的第一条就是不带 @ 的引用回复:宿主建会话要等 MCP 初始化和读库,前几次写入都可能落空。
    p = _make(fw, frequency=_Frequency(ignore_first_sets=4))
    await p.on_entry(message=_msg(_quote(BOT)))
    assert p._tracker.get("s1").boost_until is not None
    await p.on_unload()


async def test_boost_gives_up_after_retries(fw, clock, caplog):
    p = _make(fw, frequency=_Frequency(ignore_first_sets=fw.ADJUST_RETRIES))
    with caplog.at_level(logging.WARNING):
        await p.on_entry(message=_msg(_quote(BOT)))
    assert len(p.ctx.frequency.calls) == fw.ADJUST_RETRIES
    w = p._tracker.get("s1")
    assert w is not None and w.boost_until is None
    assert any(r.levelno == logging.WARNING and "[followup]" in r.getMessage() for r in caplog.records)
    await p.on_unload()


async def test_boost_retry_rereads_talk_value(fw, clock):
    class _LateRules(_Frequency):
        # 会话建出来之前宿主匹配不到群专属规则,只给全局值;建出来之后才是该群的值。
        async def set_adjust(self, chat_id, value):
            result = await super().set_adjust(chat_id, value)
            self.talk[chat_id] = 0.2
            return result

    p = _make(fw, frequency=_LateRules(talk=0.4, ignore_first_sets=1))
    await p.on_entry(message=_msg(_quote(BOT)))
    w = p._tracker.get("s1")
    assert (w.base_talk_value, w.factor) == (0.2, 5.0)
    assert p.ctx.frequency.adjust["s1"] == 5.0
    await p.on_unload()


async def test_slow_boost_in_one_group_does_not_block_another(fw, clock):
    p = _make(fw, frequency=_Frequency(ignore_first_sets=3, ignore_chats={"s1"}))
    await asyncio.gather(
        p.on_entry(message=_msg(_quote(BOT))),
        p.on_entry(message=_msg(_quote(BOT), group="20002", session="s2")),
    )
    calls = p.ctx.frequency.calls
    assert p._tracker.get("s1").boost_until is not None and p._tracker.get("s2").boost_until is not None
    assert calls.index(("s2", 2.5)) < max(i for i, c in enumerate(calls) if c[0] == "s1")
    await p.on_unload()


async def test_capability_errors_are_swallowed(fw, clock, caplog):
    class _Broken(_Frequency):
        async def get_current_talk_value(self, chat_id):
            raise RuntimeError("rpc down")

    p = _make(fw, frequency=_Broken())
    with caplog.at_level(logging.WARNING):
        await p.on_entry(message=_msg(_quote(BOT)))
    assert p._tracker.get("s1").boost_until is None
    assert any("[followup]" in r.getMessage() for r in caplog.records)
    await p.on_unload()


# ===== 过滤 =====


async def test_bot_message_does_not_open(plugin):
    await plugin.on_entry(message=_msg(AT_BOT, user=BOT))
    assert plugin._tracker.get("s1") is None


async def test_notify_message_does_not_open(plugin):
    await plugin.on_entry(message=_msg(AT_BOT, notify=True))
    assert plugin._tracker.get("s1") is None


async def test_private_message_ignored(plugin):
    await plugin.on_entry(message=_msg(_quote(BOT), group=None))
    assert plugin._tracker.get("s1") is None and plugin.ctx.frequency.calls == []


async def test_blacklisted_group_ignored(fw, clock):
    p = _make(fw, followup={"scope_mode": "blacklist", "groups": [GROUP]})
    await _open_by_at(p)
    assert p._tracker.get("s1") is None
    await _open_by_at(p, group="20002", session="s2")
    assert p._tracker.get("s2") is not None
    await p.on_unload()


async def test_disabled_plugin_ignores_at(fw, clock):
    p = _make(fw, enabled=False)
    await _open_by_at(p)
    assert p._tracker.get("s1") is None
    await p.on_unload()


async def test_group_override_durations_used(fw, clock):
    p = _make(fw, followup={"group_overrides": [{"group_id": GROUP, "window_seconds": 120, "max_active_seconds": 120}]})
    await _open_by_at(p)
    w = p._tracker.get("s1")
    assert (w.deadline, w.cap_at) == (1120, 1120)
    await p.on_unload()


# ===== 续期与退出 =====


async def test_sent_renews_active_only(plugin, clock):
    await _open_by_at(plugin)
    clock.now = 1200
    await plugin.on_sent(message=_msg([], user=BOT), sent=False)
    assert plugin._tracker.get("s1").deadline == 1300
    await plugin.on_sent(message=_msg([], user=BOT), sent=True)
    assert plugin._tracker.get("s1").deadline == 1500
    await plugin.on_sent(message=_msg([], user=BOT, session="s2"), sent=True)
    assert plugin._tracker.get("s2") is None


async def test_unboosted_window_expires_without_writing(plugin, clock):
    await _open_by_at(plugin)
    clock.now = 1299
    await plugin._tick_once()
    assert plugin._tracker.get("s1") is not None
    clock.now = 1300
    await plugin._tick_once()
    assert plugin._tracker.get("s1") is None and plugin.ctx.frequency.calls == []


async def test_boosted_window_expiry_restores(fw, clock):
    p = _make(fw, followup={"group_overrides": [{"group_id": GROUP, "window_seconds": 5, "max_active_seconds": 5}]})
    await p.on_entry(message=_msg(_quote(BOT)))
    clock.now = 1005
    await p._tick_once()
    assert p._tracker.get("s1") is None and p.ctx.frequency.adjust["s1"] == 1.0
    await p.on_unload()


async def test_failed_unboost_is_retried_next_tick(fw, clock):
    p = _make(fw, frequency=_Frequency(failing_restores=2))
    await p.on_entry(message=_msg(_quote(BOT)))
    clock.now = 1000 + plugin_boost_s(p)
    await p._tick_once()
    await p._tick_once()
    assert p.ctx.frequency.adjust["s1"] == 2.5 and p._tracker.get("s1").boost_until is not None
    await p._tick_once()
    assert p.ctx.frequency.adjust["s1"] == 1.0 and p._tracker.get("s1").boost_until is None
    await p.on_unload()


async def test_unboost_gives_up_after_retries(fw, clock, caplog):
    p = _make(fw, frequency=_Frequency(failing_restores=10_000))
    await p.on_entry(message=_msg(_quote(BOT)))
    clock.now = 1000 + plugin_boost_s(p)
    with caplog.at_level(logging.WARNING):
        for _ in range(fw.RESTORE_RETRIES):
            await p._tick_once()
    assert p._tracker.get("s1").boost_until is None
    assert len(_restores(p)) == fw.RESTORE_RETRIES
    assert any("放弃" in r.getMessage() for r in caplog.records)
    await p.on_unload()


async def test_failed_expiry_restore_is_retried(fw, clock):
    p = _make(
        fw,
        frequency=_Frequency(failing_restores=1),
        followup={"group_overrides": [{"group_id": GROUP, "window_seconds": 5, "max_active_seconds": 5}]},
    )
    await p.on_entry(message=_msg(_quote(BOT)))
    clock.now = 1005
    await p._tick_once()
    assert p._tracker.get("s1") is not None and p.ctx.frequency.adjust["s1"] == 2.5
    await p._tick_once()
    assert p._tracker.get("s1") is None and p.ctx.frequency.adjust["s1"] == 1.0
    await p.on_unload()


async def test_unload_restores_boosted_windows(plugin):
    await plugin.on_load()
    assert plugin._tick_task is not None
    await plugin.on_entry(message=_msg(_quote(BOT)))
    await _open_by_at(plugin, group="20002", session="s2")
    task = plugin._tick_task
    await plugin.on_unload()
    assert task.done() and plugin._tick_task is None
    assert _restores(plugin) == [("s1", 1.0)] and _restores(plugin, "s2") == []
    assert plugin._tracker.get("s1") is None and plugin._tracker.get("s2") is None


async def test_unload_retries_failed_restore(fw, clock):
    p = _make(fw, frequency=_Frequency(failing_restores=1))
    await p.on_entry(message=_msg(_quote(BOT)))
    await p.on_unload()
    assert p.ctx.frequency.adjust["s1"] == 1.0 and len(_restores(p)) == 2


async def test_disable_restores_all(plugin):
    await plugin.on_load()
    await plugin.on_entry(message=_msg(_quote(BOT)))
    task = plugin._tick_task
    await plugin.on_config_update("self", {"plugin": {"enabled": False, "config_version": "0.1.0"}, "followup": {}}, "")
    assert task.done() and plugin._tick_task is None
    assert plugin.ctx.frequency.adjust["s1"] == 1.0 and plugin._tracker.get("s1") is None


async def test_reenable_restarts_loop(fw, clock):
    p = _make(fw, enabled=False)
    await p.on_load()
    assert p._tick_task is None
    await p.on_config_update("self", {"plugin": {"enabled": True, "config_version": "0.1.0"}, "followup": {}}, "")
    assert p._tick_task is not None and not p._tick_task.done()
    await p.on_unload()


async def test_scope_change_closes_windows_out_of_scope(plugin):
    await plugin.on_load()
    await plugin.on_entry(message=_msg(_quote(BOT)))
    await plugin.on_entry(message=_msg(_quote(BOT), group="20002", session="s2"))
    cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "followup": {"scope_mode": "blacklist", "groups": [GROUP]},
    }
    await plugin.on_config_update("self", cfg, "")
    assert plugin._tracker.get("s1") is None and plugin.ctx.frequency.adjust["s1"] == 1.0
    assert plugin._tracker.get("s2") is not None and plugin.ctx.frequency.adjust["s2"] == 2.5
    assert plugin._tick_task is not None and not plugin._tick_task.done()


@pytest.mark.parametrize("close", ["unload", "disable"])
async def test_boost_in_flight_during_close_is_restored(fw, clock, close):
    # 冷启动后的第一条引用回复要重试几轮;这期间插件被卸载或禁用,提频收尾时已没有到期循环替它恢复。
    p = _make(fw, frequency=_Frequency(ignore_first_sets=3))
    await p.on_load()

    async def _close():
        await asyncio.sleep(0)
        if close == "unload":
            await p.on_unload()
        else:
            cfg = {"plugin": {"enabled": False, "config_version": "0.1.0"}, "followup": {}}
            await p.on_config_update("self", cfg, "")

    await asyncio.gather(p.on_entry(message=_msg(_quote(BOT))), _close())
    assert p._tracker.get("s1") is None
    assert p.ctx.frequency.adjust.get("s1", 1.0) == 1.0
    await p.on_unload()


async def test_at_racing_scope_removal_does_not_open(fw, clock):
    # 判过范围、正在查被引用消息时群被移出范围:查完不该再为它开窗。
    quoted = _msg([{"type": "text", "data": "上一句"}], user=BOT)
    p = _make(fw, message=_Message({"30001": quoted}))
    await p.on_load()
    cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "followup": {"scope_mode": "blacklist", "groups": [GROUP]},
    }
    await asyncio.gather(p.on_entry(message=_msg(_quote(None))), p.on_config_update("self", cfg, ""))
    assert p._tracker.get("s1") is None
    assert p.ctx.frequency.adjust.get("s1", 1.0) == 1.0
    await p.on_unload()


# ===== 宿主接线与启动 =====


def test_hooks_registered_as_host_expects(fw):
    # 钩子名写错或误设成 BLOCKING,插件就静默无效(或每次发送都卡到超时),而直接调用处理函数的测试都看不出来。
    comps = fw.create_plugin().get_components()
    hooks = {(c["metadata"]["hook"], c["metadata"]["mode"]) for c in comps if c["type"] == "HOOK_HANDLER"}
    assert hooks == {("chat.receive.after_process", "observe"), ("send_service.after_send", "observe")}


@pytest.mark.parametrize("enabled", [True, False])
async def test_load_logs_version(fw, clock, caplog, enabled):
    import json

    from tests.conftest import PLUGIN_DIR

    version = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))["version"]
    p = _make(fw, enabled=enabled)
    with caplog.at_level(logging.INFO):
        await p.on_load()
    assert any(f"[followup] 已加载 v{version} enabled={enabled}" in r.getMessage() for r in caplog.records)
    await p.on_unload()

import asyncio
import logging
import sys
import time
from types import SimpleNamespace

import pytest

BOT = "10001"
GROUP = "20001"


class _Message:
    def __init__(self, by_id=None, delay: float = 0) -> None:
        self.by_id = by_id or {}
        self.delay = delay
        self.lookups: list[tuple[str, str]] = []

    async def get_by_id(self, message_id, *, chat_id="", **kw):
        self.lookups.append((message_id, chat_id))
        await asyncio.sleep(self.delay)  # RPC 往返期间配置可能被改
        return self.by_id.get(message_id)


class _Ctx:
    def __init__(self, message=None) -> None:
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
PLAIN = [{"type": "text", "data": "那这个呢"}]
OPENER = "111"
OTHER = "222"


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
    return c


def _make(fw, *, enabled=True, followup=None, message=None):
    p = fw.create_plugin()
    p._ctx = _Ctx(message)
    p.set_plugin_config({"plugin": {"enabled": enabled, "config_version": "0.1.0"}, "followup": followup or {}})
    return p


@pytest.fixture
async def plugin(fw, clock):
    p = _make(fw)
    yield p
    await p.on_unload()


async def _send(p, segments, **kw):
    """返回宿主会继续使用的消息:钩子没改就是原消息。"""
    message = _msg(segments, **kw)
    result = await p.on_entry(message=message, hook_name="chat.receive.after_process")
    if result is None:
        return message
    assert result["action"] == "continue"
    assert result["modified_kwargs"]["hook_name"] == "chat.receive.after_process"
    return result["modified_kwargs"]["message"]


async def _marked(p, segments, **kw) -> bool:
    return (await _send(p, segments, **kw))["is_mentioned"] is True


def _window(p, user=OPENER, session="s1"):
    # 取插件模块里(被 clock 替换过)的当前时刻,与插件自己判断到期用的是同一个钟。
    return p._tracker.get(session, user, sys.modules[type(p).__module__].time.time())


async def _sent(p, session="s1", sent=True):
    await p.on_sent(message=_msg([], user=BOT, session=session), sent=sent)


# ===== 开窗 =====


async def test_at_opens_window_without_rewriting(plugin):
    # 宿主对 @ 本就强制触发,插件只记下窗口,不改这条消息。
    assert not await _marked(plugin, AT_BOT)
    w = _window(plugin)
    assert (w.deadline, w.cap_at) == (1300, 1900)


async def test_quote_without_at_opens_and_marks(plugin):
    # 引用回复不带 @ 时宿主不当作提及,要插件标记才会强制进 Planner。
    assert await _marked(plugin, _quote(BOT))
    assert _window(plugin) is not None and plugin.ctx.message.lookups == []


async def test_quote_resolved_via_get_by_id(fw, clock):
    quoted = _msg([{"type": "text", "data": "上一句"}], user=BOT)
    p = _make(fw, message=_Message({"30001": quoted}))
    assert await _marked(p, _quote(None))
    assert p.ctx.message.lookups == [("30001", "s1")] and _window(p) is not None


async def test_quote_to_other_user_does_not_open(fw, clock):
    other = _msg([{"type": "text", "data": "x"}], user="10002")
    p = _make(fw, message=_Message({"30001": other}))
    assert not await _marked(p, _quote("10002"))
    assert not await _marked(p, _quote(None))
    assert _window(p) is None


async def test_quote_lookup_failure_does_not_open(fw, clock):
    p = _make(fw, message=_Message({"30001": {"success": False, "error": "not found"}}))
    assert not await _marked(p, _quote(None))
    assert _window(p) is None


async def test_quote_lookup_timeout_does_not_open(fw, clock, monkeypatch):
    monkeypatch.setattr(fw, "QUOTE_LOOKUP_TIMEOUT_S", 0.01)
    quoted = _msg([{"type": "text", "data": "上一句"}], user=BOT)
    p = _make(fw, message=_Message({"30001": quoted}, delay=1))
    assert not await _marked(p, _quote(None))
    assert _window(p) is None


async def test_name_mention_does_not_open(plugin):
    await _send(plugin, [{"type": "text", "data": "麦麦在吗"}], is_mentioned=True)
    assert _window(plugin) is None


async def test_plain_message_without_window_untouched(plugin):
    assert not await _marked(plugin, PLAIN)
    assert not plugin._tracker.has_session("s1")


# ===== 窗口里只认窗口开着的人 =====


async def test_participant_followup_is_marked(plugin, clock):
    await _send(plugin, AT_BOT)
    clock.now = 1100
    assert await _marked(plugin, PLAIN)
    w = _window(plugin)
    # 追问本身不续期,只记下他刚说过话。
    assert (w.deadline, w.last_spoke_at) == (1300, 1100)


async def test_followup_already_mentioned_is_left_alone(plugin):
    await _send(plugin, AT_BOT)
    message = _msg(PLAIN, is_mentioned=True)
    assert await plugin.on_entry(message=message) is None


async def test_non_participant_message_untouched(plugin):
    await _send(plugin, AT_BOT)
    assert not await _marked(plugin, PLAIN, user=OTHER)


async def test_followup_after_expiry_untouched(plugin, clock):
    await _send(plugin, AT_BOT)
    clock.now = 1300
    assert not await _marked(plugin, PLAIN)
    assert not plugin._tracker.has_session("s1")


async def test_second_person_gets_own_window(plugin, clock):
    await _send(plugin, AT_BOT)
    clock.now = 1200
    await _send(plugin, AT_BOT, user=OTHER)
    mine, theirs = _window(plugin), _window(plugin, OTHER)
    # 别人 @ 不续我的期;他的上限从他自己开窗算。
    assert (mine.deadline, mine.cap_at) == (1300, 1900)
    assert (theirs.deadline, theirs.cap_at) == (1500, 2100)
    assert await _marked(plugin, PLAIN, user=OTHER)


async def test_reopen_renews_without_new_cap(plugin, clock):
    await _send(plugin, AT_BOT)
    for clock.now in (1250, 1500, 1750):
        await _send(plugin, _quote(BOT))
    w = _window(plugin)
    assert (w.deadline, w.cap_at) == (1900, 1900)


async def test_group_override_durations_used(fw, clock):
    p = _make(fw, followup={"group_overrides": [{"group_id": GROUP, "window_seconds": 120, "max_active_seconds": 120}]})
    await _send(p, AT_BOT)
    w = _window(p)
    assert (w.deadline, w.cap_at) == (1120, 1120)


# ===== bot 发言续期 =====


async def test_sent_renews_people_who_spoke_recently(plugin, clock):
    await _send(plugin, AT_BOT)
    clock.now = 1100
    await _send(plugin, AT_BOT, user=OTHER)
    clock.now = 1200
    await _sent(plugin, sent=False)
    assert _window(plugin).deadline == 1300
    await _sent(plugin)
    assert _window(plugin).deadline == 1500 and _window(plugin, OTHER).deadline == 1500
    # 我 1000 之后再没说话;OTHER 1350 追问过。1360 时 bot 发言只续 OTHER。
    clock.now = 1350
    await _send(plugin, PLAIN, user=OTHER)
    clock.now = 1360
    await _sent(plugin)
    assert _window(plugin).deadline == 1500 and _window(plugin, OTHER).deadline == 1660


async def test_sent_in_other_session_ignored(plugin, clock):
    await _send(plugin, AT_BOT)
    await _sent(plugin, session="s2")
    assert _window(plugin).deadline == 1300 and not plugin._tracker.has_session("s2")


async def test_sent_does_not_revive_expired(plugin, clock):
    await _send(plugin, AT_BOT)
    clock.now = 1300
    await _sent(plugin)
    assert not plugin._tracker.has_session("s1")


async def test_expiry_is_logged(plugin, clock, caplog):
    await _send(plugin, AT_BOT)
    clock.now = 1400
    with caplog.at_level(logging.INFO):
        await _send(plugin, PLAIN, user=OTHER)
    assert any("[followup] 到期" in r.getMessage() and OPENER in r.getMessage() for r in caplog.records)


# ===== 过滤 =====


async def test_bot_message_does_not_open(plugin):
    await _send(plugin, AT_BOT, user=BOT)
    assert _window(plugin, BOT) is None


async def test_notify_message_does_not_open(plugin):
    await _send(plugin, AT_BOT, notify=True)
    assert _window(plugin) is None


async def test_private_message_ignored(plugin):
    assert not await _marked(plugin, _quote(BOT), group=None)
    assert not plugin._tracker.has_session("s1")


async def test_blacklisted_group_ignored(fw, clock):
    p = _make(fw, followup={"scope_mode": "blacklist", "groups": [GROUP]})
    assert not await _marked(p, _quote(BOT))
    assert _window(p) is None
    await _send(p, AT_BOT, group="20002", session="s2")
    assert _window(p, session="s2") is not None


async def test_disabled_plugin_ignores(fw, clock):
    p = _make(fw, enabled=False)
    assert not await _marked(p, _quote(BOT))
    assert _window(p) is None


# ===== 配置变化与卸载 =====


async def test_scope_change_closes_windows_out_of_scope(plugin):
    await plugin.on_load()
    await _send(plugin, AT_BOT)
    await _send(plugin, AT_BOT, group="20002", session="s2")
    cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "followup": {"scope_mode": "blacklist", "groups": [GROUP]},
    }
    await plugin.on_config_update("self", cfg, "")
    assert not plugin._tracker.has_session("s1") and _window(plugin, session="s2") is not None


async def test_disable_clears_all(plugin):
    await _send(plugin, AT_BOT)
    await plugin.on_config_update("self", {"plugin": {"enabled": False, "config_version": "0.1.0"}, "followup": {}}, "")
    assert plugin._tracker.sessions() == []
    assert not await _marked(plugin, PLAIN)


async def test_unload_clears_and_stops_marking(plugin):
    await _send(plugin, AT_BOT)
    await plugin.on_unload()
    assert plugin._tracker.sessions() == []
    assert not await _marked(plugin, _quote(BOT))


async def test_quote_racing_scope_removal_does_not_open(fw, clock):
    # 判过范围、正在查被引用消息时群被移出范围:查完不该再为它开窗或标记。
    quoted = _msg([{"type": "text", "data": "上一句"}], user=BOT)
    p = _make(fw, message=_Message({"30001": quoted}))
    await p.on_load()
    cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "followup": {"scope_mode": "blacklist", "groups": [GROUP]},
    }
    result, _ = await asyncio.gather(p.on_entry(message=_msg(_quote(None))), p.on_config_update("self", cfg, ""))
    assert result is None and _window(p) is None


# ===== 宿主接线与启动 =====


def test_hooks_registered_as_host_expects(fw):
    # 钩子名或模式写错,插件就静默无效(观察模式的改写会被宿主丢掉),而直接调用处理函数的测试都看不出来。
    comps = fw.create_plugin().get_components()
    hooks = {
        (c["metadata"]["hook"], c["metadata"]["mode"], c["metadata"].get("timeout_ms"))
        for c in comps
        if c["type"] == "HOOK_HANDLER"
    }
    assert hooks == {
        ("chat.receive.after_process", "blocking", fw.ENTRY_TIMEOUT_MS),
        ("send_service.after_send", "observe", 0),
    }


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


async def test_readme_example_timeline(plugin, clock):
    # README「例子」一节的时间线:A 20:04 之后不再说话,bot 一直和 B 聊。
    t0 = 1000  # 20:00
    await _send(plugin, AT_BOT, user="A")
    clock.now = t0 + 120
    await _send(plugin, AT_BOT, user="B")
    clock.now = t0 + 130
    await _sent(plugin)
    clock.now = t0 + 240
    assert await _marked(plugin, PLAIN, user="A")
    clock.now = t0 + 250
    await _sent(plugin)
    for clock.now in range(t0 + 360, t0 + 1020, 60):
        await _send(plugin, PLAIN, user="B")
        await _sent(plugin)
        a = _window(plugin, "A")
        if clock.now <= t0 + 540:
            # A 20:04 说过话,20:09 之前 bot 每次发言都续他。
            assert a.deadline == clock.now + 300
        elif clock.now < t0 + 840:
            assert a.deadline == t0 + 840
        else:
            # 最后一次被续是 20:09 那次,到 20:14 关闭。
            assert a is None
    assert _window(plugin, "B").deadline == t0 + 1020  # B 的上限 20:17(从 20:02 开窗算)

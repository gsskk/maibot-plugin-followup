"""每个会话的跟进窗口:纯状态机,时间由调用方注入,不碰宿主。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Window:
    session_id: str
    group_id: str
    started_at: float
    deadline: float
    cap_at: float
    # 窗口里只有这些人的消息会让插件临时提频;开窗的人,以及窗口期间 @ 或引用回复过 bot 的人。
    participants: set[str] = field(default_factory=set)
    # 正在提频时为提频截止时刻,否则为 None;提频之外系数保持 1.0,宿主门控照常工作。
    boost_until: float | None = None
    # 最近一次提频时去掉插件系数后的原始 talk_value 与写入的系数,只用于日志对照。
    base_talk_value: float = 0.0
    factor: float = 1.0
    # 到期后恢复系数失败的次数;窗口留在表里,下一拍重试。
    restore_failures: int = 0


class WindowTracker:
    def __init__(self) -> None:
        self._windows: dict[str, Window] = {}

    def get(self, session_id: str) -> Window | None:
        return self._windows.get(session_id)

    def open(self, session_id: str, group_id: str, participant: str, now: float, window_s: int, max_s: int) -> Window:
        # 上限填得比单次窗口还短时按单次窗口算:第一次窗口不该被一个配置笔误截短。
        cap_at = now + max(max_s, window_s)
        window = Window(session_id, group_id, now, now + window_s, cap_at, participants={participant})
        self._windows[session_id] = window
        return window

    def renew(self, session_id: str, now: float, window_s: int) -> Window | None:
        """从现在起再给一个窗口,但不越过本次活跃态的上限;未活跃返回 None。"""
        window = self._windows.get(session_id)
        if window is None:
            return None
        window.deadline = min(now + window_s, window.cap_at)
        return window

    def pop(self, session_id: str) -> Window | None:
        return self._windows.pop(session_id, None)

    def windows(self) -> list[Window]:
        return list(self._windows.values())

    def put_back(self, window: Window) -> None:
        """把已弹出的窗口放回去;截止时间不变,已到期的下一拍会再处理它。"""
        self._windows[window.session_id] = window

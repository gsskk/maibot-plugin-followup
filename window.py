"""每个群里每个参与者各自的跟进窗口:纯状态机,时间由调用方注入,不碰宿主。

到期不靠定时器:读到时顺手删掉。插件不改宿主状态,过期条目晚一点删除没有副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Window:
    user_id: str
    started_at: float
    deadline: float
    # 此人这次窗口最晚到什么时候;续期不越过它,也不重新计算。
    cap_at: float
    # 此人最近一次在群里说话的时刻;bot 发言只续最近说过话的人,免得替早已离开的人一直续下去。
    last_spoke_at: float


@dataclass
class _Session:
    group_id: str
    people: dict[str, Window] = field(default_factory=dict)


class WindowTracker:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    def get(self, session_id: str, user_id: str, now: float) -> Window | None:
        """此人开着的窗口;已到期的顺手删掉并返回 None。"""
        session = self._sessions.get(session_id)
        window = session.people.get(user_id) if session else None
        if window is None:
            return None
        if window.deadline <= now:
            self._remove(session_id, user_id)
            return None
        return window

    def open_or_renew(
        self, session_id: str, group_id: str, user_id: str, now: float, window_s: int, max_s: int
    ) -> tuple[Window, bool]:
        """返回 (窗口, 是否新开)。已开着的只续此人自己,不另起上限。"""
        window = self.get(session_id, user_id, now)
        if window is not None:
            window.deadline = min(now + window_s, window.cap_at)
            window.last_spoke_at = now
            return window, False
        # 上限填得比单次窗口还短时按单次窗口算:第一次窗口不该被一个配置笔误截短。
        window = Window(user_id, now, now + window_s, now + max(max_s, window_s), now)
        session = self._sessions.setdefault(session_id, _Session(group_id))
        session.people[user_id] = window
        return window, True

    def renew_recent(self, session_id: str, now: float, window_s: int) -> list[Window]:
        """bot 发言:续最近 window_s 秒内说过话、窗口还开着的人,返回被续的窗口。"""
        session = self._sessions.get(session_id)
        if session is None:
            return []
        renewed = [w for w in session.people.values() if w.deadline > now and now - w.last_spoke_at <= window_s]
        for window in renewed:
            window.deadline = min(now + window_s, window.cap_at)
        return renewed

    def expire(self, session_id: str, now: float) -> list[Window]:
        session = self._sessions.get(session_id)
        if session is None:
            return []
        expired = [w for w in session.people.values() if w.deadline <= now]
        for window in expired:
            self._remove(session_id, window.user_id)
        return expired

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def group_of(self, session_id: str) -> str | None:
        session = self._sessions.get(session_id)
        return session.group_id if session else None

    def sessions(self) -> list[str]:
        return list(self._sessions)

    def drop_session(self, session_id: str) -> int:
        session = self._sessions.pop(session_id, None)
        return len(session.people) if session else 0

    def clear(self) -> None:
        self._sessions.clear()

    def _remove(self, session_id: str, user_id: str) -> None:
        session = self._sessions[session_id]
        session.people.pop(user_id, None)
        if not session.people:
            del self._sessions[session_id]

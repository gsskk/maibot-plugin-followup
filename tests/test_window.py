import pytest


@pytest.fixture
def WindowTracker(fw):
    return fw.window.WindowTracker


def _open(t, user, now, session="s1", window_s=300, max_s=900):
    return t.open_or_renew(session, "g1", user, now, window_s, max_s)


def test_open_sets_deadline_cap_and_last_spoke(WindowTracker):
    w, opened = _open(WindowTracker(), "u1", 1000)
    assert opened and (w.deadline, w.cap_at, w.last_spoke_at) == (1300, 1900, 1000)


def test_cap_never_shorter_than_window(WindowTracker):
    w, _ = _open(WindowTracker(), "u1", 1000, max_s=120)
    assert (w.deadline, w.cap_at) == (1300, 1300)


def test_reopen_renews_only_that_person_within_own_cap(WindowTracker):
    t = WindowTracker()
    _open(t, "u1", 1000)
    _open(t, "u2", 1200)
    w1, opened = _open(t, "u1", 1250)
    assert not opened and (w1.deadline, w1.cap_at, w1.last_spoke_at) == (1550, 1900, 1250)
    assert t.get("s1", "u2", 1250).deadline == 1500
    _open(t, "u1", 1500)
    w1, opened = _open(t, "u1", 1750)
    assert not opened and w1.deadline == 1900


def test_reopen_after_expiry_starts_fresh_cap(WindowTracker):
    t = WindowTracker()
    _open(t, "u1", 1000)
    w, opened = _open(t, "u1", 1300)
    assert opened and (w.deadline, w.cap_at) == (1600, 2200)


def test_each_person_has_own_cap(WindowTracker):
    t = WindowTracker()
    _open(t, "u1", 1000)
    w2, _ = _open(t, "u2", 1200)
    assert w2.cap_at == 2100


def test_get_drops_expired(WindowTracker):
    t = WindowTracker()
    _open(t, "u1", 1000)
    assert t.get("s1", "u1", 1299) is not None
    assert t.get("s1", "u1", 1300) is None
    assert not t.has_session("s1")


def test_renew_recent_skips_people_quiet_for_a_window(WindowTracker):
    t = WindowTracker()
    _open(t, "u1", 1000)
    _open(t, "u2", 1200)
    # u1 最后说话在 250 秒前,u2 在 50 秒前:都续;到 1301 时 u1 已过期,不复活。
    renewed = t.renew_recent("s1", 1250, 300)
    assert sorted(w.user_id for w in renewed) == ["u1", "u2"]
    assert t.get("s1", "u1", 1250).deadline == 1550
    t.get("s1", "u2", 1250).last_spoke_at = 1500
    renewed = t.renew_recent("s1", 1520, 300)
    # u1 最后说话 520 秒前:不续,截止仍是 1550;u2 续到 1820。
    assert [w.user_id for w in renewed] == ["u2"]
    assert t.get("s1", "u1", 1520).deadline == 1550 and t.get("s1", "u2", 1520).deadline == 1820


def test_renew_recent_respects_cap_and_ignores_expired(WindowTracker):
    t = WindowTracker()
    w, _ = _open(t, "u1", 1000, window_s=300, max_s=400)
    w.last_spoke_at = 1290
    assert t.renew_recent("s1", 1295, 300)[0].deadline == 1400
    assert t.renew_recent("s1", 1400, 300) == []


def test_expire_returns_and_removes(WindowTracker):
    t = WindowTracker()
    _open(t, "u1", 1000)
    _open(t, "u2", 1100)
    gone = t.expire("s1", 1350)
    assert [w.user_id for w in gone] == ["u1"] and t.has_session("s1")
    assert [w.user_id for w in t.expire("s1", 1400)] == ["u2"] and not t.has_session("s1")
    assert t.expire("nope", 0) == []


def test_sessions_group_and_drop(WindowTracker):
    t = WindowTracker()
    _open(t, "u1", 0, session="a")
    _open(t, "u2", 0, session="b")
    _open(t, "u3", 0, session="b")
    assert sorted(t.sessions()) == ["a", "b"] and t.group_of("a") == "g1" and t.group_of("x") is None
    assert t.drop_session("b") == 2 and t.drop_session("b") == 0
    t.clear()
    assert t.sessions() == []

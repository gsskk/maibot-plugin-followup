import pytest


@pytest.fixture
def WindowTracker(fw):
    return fw.window.WindowTracker


def test_open_sets_deadline_and_cap(WindowTracker):
    w = WindowTracker().open("s1", "g1", "u1", now=1000, window_s=300, max_s=900)
    assert (w.deadline, w.cap_at) == (1300, 1900)


def test_open_records_opener_as_participant_and_no_boost(WindowTracker):
    w = WindowTracker().open("s1", "g1", "u1", now=1000, window_s=300, max_s=900)
    assert w.participants == {"u1"}
    assert w.boost_until is None and w.factor == 1.0


def test_renew_extends_from_now(WindowTracker):
    t = WindowTracker()
    t.open("s1", "g1", "u1", now=1000, window_s=300, max_s=900)
    assert t.renew("s1", now=1200, window_s=300).deadline == 1500


def test_renew_capped(WindowTracker):
    t = WindowTracker()
    t.open("s1", "g1", "u1", now=1000, window_s=300, max_s=900)
    assert t.renew("s1", now=1800, window_s=300).deadline == 1900


def test_cap_never_shorter_than_window(WindowTracker):
    w = WindowTracker().open("s1", "g1", "u1", now=1000, window_s=300, max_s=120)
    assert (w.deadline, w.cap_at) == (1300, 1300)


def test_renew_inactive_returns_none(WindowTracker):
    assert WindowTracker().renew("nope", now=1, window_s=300) is None


def test_pop_and_windows(WindowTracker):
    t = WindowTracker()
    t.open("a", "g", "u1", now=0, window_s=10, max_s=10)
    t.open("b", "g", "u2", now=0, window_s=100, max_s=100)
    assert sorted(w.session_id for w in t.windows()) == ["a", "b"]
    assert t.pop("a").session_id == "a" and t.get("a") is None and t.pop("a") is None


def test_put_back_restores_popped_window(WindowTracker):
    t = WindowTracker()
    w = t.open("a", "g", "u1", now=0, window_s=10, max_s=10)
    t.pop("a")
    t.put_back(w)
    assert t.get("a") is w

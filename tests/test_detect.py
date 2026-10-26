import pytest

BOT = "10001"


def _msg(segments, *, user="111", account=BOT, group="20001", additional_config=None, notify=False):
    return {
        "message_id": "m1",
        "platform": "qq",
        "account_id": account,
        "session_id": "s1",
        "message_info": {
            "user_info": {"user_id": user, "user_nickname": "u", "user_cardname": None},
            "group_info": {"group_id": group, "group_name": "g"} if group is not None else None,
            "additional_config": additional_config or {},
        },
        "raw_message": segments,
        "is_mentioned": False,
        "is_at": False,
        "is_notify": notify,
    }


@pytest.fixture
def detect(fw):
    return fw.detect


def test_at_component_targeting_bot(detect):
    assert detect.at_targets_bot(_msg([{"type": "at", "data": {"target_user_id": BOT}}]))


def test_at_other_user_is_not_at_bot(detect):
    assert not detect.at_targets_bot(_msg([{"type": "at", "data": {"target_user_id": "10002"}}]))


def test_additional_config_at_bot(detect):
    assert detect.at_targets_bot(_msg([], additional_config={"at_bot": True}))


def test_mention_bot_segment(detect):
    assert detect.at_targets_bot(_msg([{"type": "mention_bot", "data": None}]))


def test_mention_bot_nested_in_seglist(detect):
    nested = {"type": "seglist", "data": [{"type": "text", "data": "x"}, {"type": "mention_bot", "data": None}]}
    assert detect.at_targets_bot(_msg([nested]))


def test_quote_returns_target(detect):
    segs = [
        {"type": "reply", "data": {"target_message_id": "30001", "target_message_sender_id": None}},
        {"type": "text", "data": "这算什么?"},
    ]
    assert detect.quoted_message(_msg(segs)) == ("30001", None)


def test_no_quote_returns_none(detect):
    assert detect.quoted_message(_msg([{"type": "text", "data": "hi"}])) is None


def test_ignores_self_echo(detect):
    assert detect.is_from_bot(_msg([], user=BOT))
    assert not detect.is_from_bot(_msg([]))


def test_private_has_no_group(detect):
    assert detect.group_id_of(_msg([], group=None)) == ""
    assert detect.group_id_of(_msg([])) == "20001"


def test_account_missing_never_matches(detect):
    m = _msg([{"type": "at", "data": {"target_user_id": ""}}], account="", user="")
    assert not detect.at_targets_bot(m)
    assert not detect.is_from_bot(m)


def test_malformed_message_is_false_not_error(detect):
    for bad in ({}, {"message_info": None, "raw_message": None}, {"raw_message": [None, "x", {"type": "at"}]}):
        assert not detect.at_targets_bot(bad)
        assert detect.quoted_message(bad) is None
        assert detect.group_id_of(bad) == ""
        assert not detect.is_from_bot(bad)

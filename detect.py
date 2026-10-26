"""判断一条序列化 SessionMessage 是不是冲着 bot 来的 @ 或引用回复。

只认 @,不认名字提及:叫名字不开跟进窗口。字段缺失或形状不对一律当作没命中,不抛异常——
钩子收到的是宿主跨进程传来的字典,不同适配器填得并不齐。
"""

from __future__ import annotations

from typing import Any


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _segments(message: dict) -> list[dict]:
    raw = _dict(message).get("raw_message")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _info(message: dict) -> dict:
    return _dict(_dict(message).get("message_info"))


def _bot_account(message: dict) -> str:
    return str(_dict(message).get("account_id") or "").strip()


def group_id_of(message: dict) -> str:
    """群号;私聊或缺字段返回空串。"""
    return str(_dict(_info(message).get("group_info")).get("group_id") or "").strip()


def is_from_bot(message: dict) -> bool:
    """bot 自己发的消息经入站回显时,发送者就是收消息的账号。"""
    account = _bot_account(message)
    user_id = str(_dict(_info(message).get("user_info")).get("user_id") or "").strip()
    return bool(account) and user_id == account


def _has_mention_bot(segments: list[dict]) -> bool:
    for seg in segments:
        if seg.get("type") == "mention_bot":
            return True
        data = seg.get("data")
        if (
            seg.get("type") == "seglist"
            and isinstance(data, list)
            and _has_mention_bot([s for s in data if isinstance(s, dict)])
        ):
            return True
    return False


def at_targets_bot(message: dict) -> bool:
    """与宿主 is_mentioned_bot_in_message 的 @ 判据一致(不含名字提及)。"""
    if _dict(_info(message).get("additional_config")).get("at_bot"):
        return True
    segments = _segments(message)
    if _has_mention_bot(segments):
        return True
    account = _bot_account(message)
    if not account:
        return False
    return any(
        seg.get("type") == "at" and str(_dict(seg.get("data")).get("target_user_id") or "").strip() == account
        for seg in segments
    )


def quoted_message(message: dict) -> tuple[str, str | None] | None:
    """引用段的 (被引用消息 id, 被引用者账号);被引用者可能为 None,需要调用方另查。无引用返回 None。"""
    for seg in _segments(message):
        if seg.get("type") != "reply":
            continue
        data = _dict(seg.get("data"))
        target_id = str(data.get("target_message_id") or "").strip()
        if not target_id:
            continue
        sender = data.get("target_message_sender_id")
        sender = str(sender).strip() if sender not in (None, "") else None
        return target_id, sender or None
    return None

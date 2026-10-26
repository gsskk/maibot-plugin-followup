"""插件配置与按群解析。

WebUI 只显示字段的 label 与 hint,不显示 description;每个字段都要经 _field() 声明,否则页面上只剩英文字段名。
配置节的说明取自类的 docstring。
"""

from __future__ import annotations

from typing import Any

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

SCOPE_MODES = ("all", "blacklist", "whitelist")


def _group_str(value: Any) -> Any:
    """TOML 里不加引号的群号是整数;按字符串收下,而不是让整份配置校验失败、插件不启动。"""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _field(default: Any = None, *, label: str, hint: str = "", default_factory: Any = None, **ui: Any) -> Any:
    value = {"default_factory": default_factory} if default_factory is not None else {"default": default}
    return Field(**value, description=hint or label, json_schema_extra={"label": label, "hint": hint or None, **ui})


class PluginSection(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = _field(False, label="启用插件")
    config_version: str = _field("0.1.0", label="配置版本", hint="由插件维护,不要手改", disabled=True)


class GroupOverride(PluginConfigBase):
    group_id: str = _field("", label="群号", placeholder="裸群号")
    window_seconds: int = _field(0, label="窗口时长(秒)", placeholder="0 = 沿用默认")
    max_active_seconds: int = _field(0, label="活跃上限(秒)", placeholder="0 = 沿用默认")

    @field_validator("group_id", mode="before")
    @classmethod
    def _coerce_group_id(cls, value: Any) -> Any:
        return _group_str(value)


class FollowupSection(PluginConfigBase):
    """被 @ 或被引用回复后,该群在一段时间内的每批消息都交给 Planner 判断;只有 bot 自己发言才续期。"""

    __ui_label__ = "跟进窗口"
    __ui_order__ = 1

    # 不用 Literal:WebUI 的下拉框只认 Literal,但那样填错的值会让整份配置校验失败;这里填错按 all 处理并报出来。
    scope_mode: str = _field(
        "all",
        label="范围模式",
        hint="all = 所有群;blacklist = 名单里的群关闭;whitelist = 只有名单里的群开启",
    )
    groups: list[str] = _field(
        default_factory=list, label="群名单", hint="裸群号;blacklist 模式下关闭、whitelist 模式下开启,all 模式忽略"
    )
    window_seconds: int = _field(300, label="窗口时长(秒)", hint="被 @ 或 bot 发言后,活跃态再持续这么久")
    max_active_seconds: int = _field(
        900, label="活跃上限(秒)", hint="一次活跃态从开始算最长持续多久;等于窗口时长即不续期"
    )
    group_overrides: list[GroupOverride] = _field(
        default_factory=list, label="按群覆盖时长", hint="每行一个群;留 0 的项沿用上面的默认"
    )

    @field_validator("groups", mode="before")
    @classmethod
    def _coerce_groups(cls, value: Any) -> Any:
        return [_group_str(v) for v in value] if isinstance(value, list) else value


class FollowupConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    followup: FollowupSection = Field(default_factory=FollowupSection)


def _mode(cfg: FollowupConfig) -> str:
    mode = cfg.followup.scope_mode.strip().lower()
    return mode if mode in SCOPE_MODES else "all"


def group_enabled(cfg: FollowupConfig, group_id: str) -> bool:
    mode = _mode(cfg)
    if mode == "all":
        return True
    listed = group_id.strip() in {g.strip() for g in cfg.followup.groups}
    return listed if mode == "whitelist" else not listed


def _override(cfg: FollowupConfig, group_id: str) -> GroupOverride | None:
    """重复的群以后一行为准(validate 会报出来)。"""
    found = None
    for o in cfg.followup.group_overrides:
        if o.group_id.strip() == group_id.strip():
            found = o
    return found


def durations_for(cfg: FollowupConfig, group_id: str) -> tuple[int, int]:
    """(window_s, max_s)。非正数一律回落:先到全局值,全局值本身非正就到字段默认值。"""
    window_s = cfg.followup.window_seconds if cfg.followup.window_seconds > 0 else 300
    max_s = cfg.followup.max_active_seconds if cfg.followup.max_active_seconds > 0 else 900
    o = _override(cfg, group_id)
    if o is not None:
        window_s = o.window_seconds if o.window_seconds > 0 else window_s
        max_s = o.max_active_seconds if o.max_active_seconds > 0 else max_s
    return window_s, max_s


def validate(cfg: FollowupConfig) -> list[str]:
    """人读的问题列表,只用于日志;有问题的项按 durations_for / group_enabled 的回落规则继续工作。"""
    f = cfg.followup
    problems: list[str] = []
    if f.scope_mode.strip().lower() not in SCOPE_MODES:
        problems.append(f"范围模式 {f.scope_mode!r} 无法识别,按 all 处理")
    if f.window_seconds <= 0:
        problems.append(f"窗口时长 {f.window_seconds} 不是正数,按 300 秒处理")
    if f.max_active_seconds <= 0:
        problems.append(f"活跃上限 {f.max_active_seconds} 不是正数,按 900 秒处理")
    window_s, max_s = durations_for(cfg, "")
    if max_s < window_s:
        problems.append(f"活跃上限 {max_s} 秒短于窗口时长 {window_s} 秒,按窗口时长算(即不续期)")
    seen: set[str] = set()
    for i, o in enumerate(f.group_overrides, start=1):
        gid = o.group_id.strip()
        if not gid:
            problems.append(f"按群覆盖第 {i} 行没有填群号,已忽略")
            continue
        if gid in seen:
            problems.append(f"按群覆盖里群 {gid} 出现了不止一次,以最后一行为准")
        seen.add(gid)
        if o.window_seconds < 0 or o.max_active_seconds < 0:
            problems.append(f"按群覆盖里群 {gid} 的时长是负数,按 0(沿用默认)处理")
    # 重复的群只看最后一行,与 durations_for 一致。
    for gid in seen:
        o_window, o_max = durations_for(cfg, gid)
        if (o_window, o_max) != (window_s, max_s) and o_max < o_window:
            problems.append(f"群 {gid} 的活跃上限 {o_max} 秒短于窗口时长 {o_window} 秒,按窗口时长算(即不续期)")
    return problems

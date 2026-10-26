import pytest


@pytest.fixture
def config(fw):
    return fw.config


def _cfg(config, **followup):
    return config.FollowupConfig.model_validate({"plugin": {"enabled": True}, "followup": followup})


def test_defaults(config):
    cfg = config.FollowupConfig()
    assert cfg.plugin.enabled is False
    assert (cfg.followup.scope_mode, cfg.followup.groups) == ("all", [])
    assert (cfg.followup.window_seconds, cfg.followup.max_active_seconds) == (300, 900)


def test_all_mode_enables_any_group(config):
    cfg = _cfg(config, scope_mode="all", groups=["1"])
    assert config.group_enabled(cfg, "1") and config.group_enabled(cfg, "2")


def test_blacklist_mode(config):
    cfg = _cfg(config, scope_mode="blacklist", groups=[" 20002 "])
    assert not config.group_enabled(cfg, "20002")
    assert config.group_enabled(cfg, "20001")


def test_whitelist_mode(config):
    cfg = _cfg(config, scope_mode="whitelist", groups=["20001"])
    assert config.group_enabled(cfg, "20001")
    assert not config.group_enabled(cfg, "20002")


def test_durations_default(config):
    assert config.durations_for(_cfg(config), "20001") == (300, 900)


def test_durations_override_partial(config):
    cfg = _cfg(config, group_overrides=[{"group_id": "20002", "window_seconds": 120}])
    assert config.durations_for(cfg, "20002") == (120, 900)
    assert config.durations_for(cfg, "20001") == (300, 900)


def test_durations_override_full(config):
    cfg = _cfg(config, group_overrides=[{"group_id": "20002", "window_seconds": 120, "max_active_seconds": 120}])
    assert config.durations_for(cfg, "20002") == (120, 120)


def test_validate_reports_unknown_mode_and_duplicates(config):
    cfg = _cfg(
        config,
        scope_mode="foo",
        groups=["20001"],
        group_overrides=[{"group_id": "1", "window_seconds": 60}, {"group_id": "1", "window_seconds": 90}],
    )
    problems = config.validate(cfg)
    assert len(problems) == 2
    assert any("foo" in p for p in problems)
    # 未知模式按 all 处理:名单被忽略,任何群都开启。
    assert config.group_enabled(cfg, "20002")


def test_validate_reports_nonpositive_durations(config):
    cfg = _cfg(config, window_seconds=0)
    assert len(config.validate(cfg)) == 1
    assert config.durations_for(cfg, "1") == (300, 900)


def test_validate_clean_config_has_no_problems(config):
    assert config.validate(_cfg(config, scope_mode="blacklist", groups=["1"])) == []


def test_bare_integer_group_ids_are_accepted(config):
    # TOML 里最顺手的写法是不加引号的群号;不能因此让整份配置校验失败、插件不启动。
    cfg = _cfg(
        config,
        scope_mode="whitelist",
        groups=[20001],
        group_overrides=[{"group_id": 20002, "window_seconds": 120}],
    )
    assert cfg.followup.groups == ["20001"]
    assert config.group_enabled(cfg, "20001")
    assert config.durations_for(cfg, "20002") == (120, 900)


def test_validate_reports_cap_shorter_than_window(config):
    cfg = _cfg(config, window_seconds=300, max_active_seconds=120)
    problems = config.validate(cfg)
    assert len(problems) == 1 and "120" in problems[0] and "300" in problems[0]


def test_validate_reports_override_cap_shorter_than_window(config):
    # 覆盖只填了一半时按实际生效的值比较:这里生效的是 (1200, 900)。
    cfg = _cfg(config, group_overrides=[{"group_id": "20001", "window_seconds": 1200, "max_active_seconds": 0}])
    problems = config.validate(cfg)
    assert len(problems) == 1 and "20001" in problems[0]


def test_validate_reports_negative_override(config):
    cfg = _cfg(config, group_overrides=[{"group_id": "20001", "window_seconds": -5}])
    problems = config.validate(cfg)
    assert len(problems) == 1 and "20001" in problems[0]
    assert config.durations_for(cfg, "20001") == (300, 900)

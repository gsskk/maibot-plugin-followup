"""按宿主方式加载插件目录:以 plugin.py 为入口的 spec_from_file_location + submodule_search_locations,
插件内的相对导入才能解析。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PACKAGE = "_fw_test_plugin"
SUBMODULES = ("window", "detect", "config")


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, str(PLUGIN_DIR / "plugin.py"), submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    try:
        spec.loader.exec_module(module)
        for name in SUBMODULES:
            importlib.import_module(f"{PACKAGE}.{name}")
    except BaseException:
        # 半加载的包留在 sys.modules 里,后面的测试会报莫名其妙的 AttributeError 而不是真正的导入错误。
        sys.modules.pop(PACKAGE, None)
        raise
    return module


@pytest.fixture
def fw():
    """宿主把 plugin.py 本身当作包模块:create_plugin 等直接挂在 fw 上,其余子模块经 fw.window 等访问。"""
    return _load_plugin_package()

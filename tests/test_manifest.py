"""宿主 manifest 校验(manifest_validator.PluginManifest)不通过时整个插件目录被跳过,只在宿主日志里留一行。"""

import json
import re

from tests.conftest import PLUGIN_DIR


def _manifest() -> dict:
    return json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))


def test_manifest_has_host_required_keys():
    m = _manifest()
    required = {
        "manifest_version",
        "id",
        "version",
        "name",
        "description",
        "author",
        "license",
        "urls",
        "host_application",
        "sdk",
        "capabilities",
        "i18n",
    }
    assert required <= m.keys(), sorted(required - m.keys())
    assert m["author"].get("name") and m["author"].get("url")
    assert re.match(r"^https?://", m["urls"].get("repository", ""))


def test_manifest_declares_capabilities_the_plugin_calls():
    assert set(_manifest()["capabilities"]) == {
        "frequency.get_current_talk_value",
        "frequency.get_adjust",
        "frequency.set_adjust",
        "message.get_by_id",
    }


def test_repository_root_has_required_official_files():
    assert (PLUGIN_DIR / "_manifest.json").is_file()
    assert (PLUGIN_DIR / "plugin.py").is_file()
    license_file = PLUGIN_DIR / "LICENSE"
    assert license_file.is_file()
    assert "MIT License" in license_file.read_text(encoding="utf-8")

"""固定配置只从明确位置读取，不混入账号凭据或发布材料。"""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fgo_local_exporter import game_profile
from fgo_local_exporter.desktop import create_desktop_app
from fgo_local_exporter.desktop_bridge import DesktopBridge
from fgo_local_exporter.desktop_resources import ResourceManager
from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.settings import LocalConfig


@pytest.fixture
def config_value():
    return {
        "version": "2.129.0",
        "sdk_base": {
            "merchant_id": "80001", "game_id": "80002", "app_id": "80003",
            "server_id": "80004", "version": "80005", "sdk_type": "80006",
        },
        "sdk_key": "SyntheticSDKKey1234567890",
        "development_code": "SyntheticDevelopment123456",
        "session_prefix": "SyntheticSessionPrefix123456",
    }


def test_source_configuration_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    expected = Path(game_profile.__file__).resolve().parents[2] / "game-protocol.json"
    assert game_profile.default_profile_path() == expected


def test_executable_inside_project_uses_project_configuration(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").touch()
    module = tmp_path / "src/fgo_local_exporter/game_profile.py"
    module.parent.mkdir(parents=True)
    module.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "dist/app/FGOStatDesktop.exe"))
    assert game_profile.default_profile_path() == tmp_path / "game-protocol.json"


def test_portable_executable_uses_its_own_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "FGOStatDesktop.exe"))
    assert game_profile.default_profile_path() == tmp_path / "game-protocol.json"


@pytest.fixture
def bundled_desktop(tmp_path, monkeypatch, config_value):
    directory = tmp_path / "unpacked/runtime-config"
    directory.mkdir(parents=True)
    (directory / "game-protocol.json").write_text(json.dumps(config_value), encoding="utf-8")
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    monkeypatch.chdir(standalone)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(directory.parent), raising=False)
    monkeypatch.setattr(sys, "executable", str(standalone / "FGOStatDesktop.exe"))
    return directory / "game-protocol.json"


def test_standalone_desktop_loads_bundled_profile_before_first_page(
    tmp_path, bundled_desktop, config_value
):
    config = LocalConfig(origin="http://127.0.0.1:55313")
    bridge = DesktopBridge(config, None)
    app = create_desktop_app(config, bridge)
    try:
        client = app.test_client()
        client.get("/", base_url=config.origin)
        state = client.get("/api/state", base_url=config.origin).json
        assert state["qr_available"] and state["can_start"]
        assert state["qr_error"] == ""
        assert bridge.status()["game_phase"] == "ready"
        assert game_profile.profile_values(bridge._store.profile) == config_value
        exposed = json.dumps(state) + json.dumps(bridge.status())
        for key in ("sdk_key", "development_code", "session_prefix"):
            assert config_value[key] not in exposed
        for url in ("/runtime-config/game-protocol.json", "/static/game-protocol.json"):
            assert client.get(url, base_url=config.origin).status_code == 404
    finally:
        app.extensions["local_store"].close()


def test_explicit_profile_overrides_bundle(tmp_path, bundled_desktop, config_value):
    selected = tmp_path / "custom.json"
    config_value["session_prefix"] = "SyntheticExplicitPrefix123456"
    selected.write_text(json.dumps(config_value), encoding="utf-8")
    assert game_profile.profile_values(game_profile.load_desktop_profile(selected)) == config_value
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_REQUIRED$"):
        game_profile.load_desktop_profile(tmp_path / "missing.json")
    selected.write_text("synthetic-invalid-config", encoding="utf-8")
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_INVALID$"):
        game_profile.load_desktop_profile(selected)


def test_invalid_bundle_does_not_fall_back_to_external_profile(
    bundled_desktop, config_value
):
    config_value["cookie"] = "SYNTHETIC_ACCOUNT_VALUE"
    bundled_desktop.write_text(json.dumps(config_value), encoding="utf-8")
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_INVALID$"):
        game_profile.load_desktop_profile()


def test_desktop_build_requires_a_checked_profile(tmp_path, monkeypatch, config_value):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    runtime = importlib.import_module("release_parts.runtime")
    with pytest.raises(RuntimeError, match="缺少扫码固定配置"):
        runtime.desktop_build_profile(tmp_path)
    path = tmp_path / "game-protocol.json"
    path.write_text(json.dumps(config_value), encoding="utf-8")
    profile = runtime.desktop_build_profile(tmp_path)
    staged = runtime.stage_desktop_profile(tmp_path, profile)
    assert json.loads(staged.read_bytes()) == config_value
    assert list(staged.parent.iterdir()) == [staged]
    config_value["access_key"] = "SYNTHETIC_ACCOUNT_VALUE"
    path.write_text(json.dumps(config_value), encoding="utf-8")
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_INVALID$"):
        runtime.desktop_build_profile(tmp_path)


def test_missing_configuration_does_not_disable_file_import(tmp_path):
    assert game_profile.load_profile(tmp_path / "game-protocol.json") is None


def test_configuration_changes_are_read_without_a_second_cache(tmp_path, config_value):
    path = tmp_path / "game-protocol.json"
    path.write_text(json.dumps(config_value), encoding="utf-8-sig")
    assert game_profile.profile_values(game_profile.load_profile(path)) == config_value
    config_value["session_prefix"] = "SyntheticUpdatedPrefix123456"
    path.write_text(json.dumps(config_value), encoding="utf-8")
    assert game_profile.load_profile(path).session_prefix == config_value["session_prefix"]


@pytest.mark.parametrize("extra", ["token", "session", "cookie", "password", "usk"])
def test_account_fields_are_rejected_from_fixed_configuration(tmp_path, config_value, extra):
    config_value[extra] = "SYNTHETIC_ACCOUNT_VALUE"
    path = tmp_path / "game-protocol.json"
    path.write_text(json.dumps(config_value), encoding="utf-8")
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_INVALID$") as error:
        game_profile.load_profile(path)
    assert "SYNTHETIC_ACCOUNT_VALUE" not in str(error.value)


@pytest.mark.parametrize("invalid", ["https://invalid.example/", "", "9" * 17, 123, None])
def test_sdk_base_rejects_non_numeric_or_unbounded_values(config_value, invalid):
    config_value["sdk_base"]["game_id"] = invalid
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_INVALID$"):
        game_profile.checked_profile(config_value)


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_sdk_base_requires_exact_field_set(config_value, change):
    if change == "missing":
        del config_value["sdk_base"]["game_id"]
    else:
        config_value["sdk_base"]["unexpected"] = "80007"
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_INVALID$"):
        game_profile.checked_profile(config_value)


@pytest.mark.parametrize("raw", [b"", b"not-json-private-value", b" " * 16385, b"[]"])
def test_invalid_configuration_returns_only_a_stable_error(tmp_path, raw):
    path = tmp_path / "game-protocol.json"
    path.write_bytes(raw)
    with pytest.raises(ExportError, match="^LOCAL_PROFILE_INVALID$"):
        game_profile.load_profile(path)


def test_desktop_loads_the_same_json_and_does_not_serve_it(tmp_path, config_value):
    path = tmp_path / "game-protocol.json"
    path.write_text(json.dumps(config_value), encoding="utf-8")
    config = LocalConfig(origin="http://127.0.0.1:55313")
    bridge = DesktopBridge(config, ResourceManager(tmp_path / "resources"), profile_path=path)
    app = create_desktop_app(config, bridge)
    try:
        bridge._restore_profile()
        assert bridge.status()["game_phase"] == "ready"
        assert game_profile.profile_values(bridge._store.profile) == config_value
        client = app.test_client()
        for url in ("/game-protocol.json", "/static/game-protocol.json"):
            result = client.get(url, base_url=config.origin)
            assert result.status_code == 404
            assert config_value["sdk_key"].encode() not in result.data
    finally:
        app.extensions["local_store"].close()


def test_android_build_defaults_to_the_project_json(tmp_path, monkeypatch, config_value):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    builder = importlib.import_module("prepare_android")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    assert builder.build_profile() is None
    path = tmp_path / "game-protocol.json"
    path.write_text(json.dumps(config_value), encoding="utf-8")
    assert game_profile.profile_values(builder.build_profile()) == config_value
    with pytest.raises(RuntimeError, match="指定的游戏固定配置文件不存在"):
        builder.build_profile(tmp_path / "missing.json")


def test_private_configuration_files_are_ignored_by_git():
    root = Path(__file__).parents[1]
    names = ["game-protocol.json", "protocol.dpapi", "protocol.json"]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=root, input=("\0".join(names) + "\0").encode(), capture_output=True, check=True,
    )
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(names)

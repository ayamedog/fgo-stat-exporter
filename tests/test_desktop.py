import base64
import io
import json
import os
import re
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from test_desktop_storage import MemorySettings

from fgo_local_exporter.desktop import create_desktop_app
from fgo_local_exporter.desktop_bridge import DesktopBridge
from fgo_local_exporter.desktop_profile import load_cached_profile, save_cached_profile
from fgo_local_exporter.desktop_resources import ResourceManager
from fgo_local_exporter.desktop_storage import read_startup_directory
from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.game_profile import GameProfile
from fgo_local_exporter.settings import LocalConfig


def profile():
    # 仅为格式校验构造参数，不来自游戏或账号。
    return GameProfile(
        "2.129.0",
        {
            "merchant_id": "1",
            "game_id": "112",
            "app_id": "112",
            "server_id": "248",
            "version": "3",
            "sdk_type": "1",
        },
        "SyntheticSDKKey1234567890",
        "SyntheticDevelopment123456",
        "SyntheticSessionPrefix123456",
    )


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 需要 Windows 当前用户")
def test_protocol_cache_is_encrypted_and_excludes_account_fields(tmp_path):
    path = tmp_path / "protocol.dpapi"
    original = profile()
    save_cached_profile(path, original)
    raw = path.read_bytes()
    assert original.sdk_key.encode() not in raw
    assert original.development_code.encode() not in raw
    assert original.session_prefix.encode() not in raw
    restored = load_cached_profile(path)
    assert restored.sdk_key == original.sdk_key
    assert restored.session_prefix == original.session_prefix
    assert set(restored.__dict__) == {
        "version",
        "sdk_base",
        "sdk_key",
        "development_code",
        "session_prefix",
    }


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 需要 Windows 当前用户")
def test_corrupted_protocol_cache_fails_without_echoing_data(tmp_path):
    path = tmp_path / "protocol.dpapi"
    path.write_bytes(b"FGOP1\0not-an-encrypted-game-profile")
    with pytest.raises(ExportError) as error:
        load_cached_profile(path)
    assert error.value.code == "LOCAL_PROFILE_ENCRYPTION_FAILED"
    assert "not-an-encrypted" not in str(error.value)


def make_app(tmp_path, *, configured=True):
    resources = ResourceManager(tmp_path / "resources") if configured else None
    config = LocalConfig(origin="http://127.0.0.1:55313")
    bridge = DesktopBridge(config, resources)
    bridge._profile_path = tmp_path / "game-protocol.json"
    app = create_desktop_app(config, bridge)
    app.config["SERVER_NAME"] = config.host
    client = app.test_client()
    page = client.get("/").get_data(as_text=True)
    csrf = re.search(r'name="fgo-csrf" content="([^"]+)"', page)[1]
    return app, client, bridge, page, csrf


def test_desktop_page_uses_local_privacy_text_without_automatic_emulator_read(tmp_path):
    app, client, bridge, page, csrf = make_app(tmp_path)
    try:
        assert "数据仅在本机临时处理" in page
        assert "desktop-login-state" not in page
        assert "字典保存位置" in page
        assert "下载统计 JSON（可导入 Chaldea）" in page
        assert "首次读取原版 APK" not in page
        assert "从模拟器读取" not in page
        assert 'id="desktop-progress"' in page
        assert client.get("/desktop.css").status_code == 200
        assert "服务器内存中的统计数据" not in page
        script = client.get("/desktop.js").get_data(as_text=True)
        assert 'if (initial.game_phase === "idle")' not in script
        assert bridge.connect(csrf)["ok"]
        assert not bridge.connect("invalid-token")["ok"]
    finally:
        app.extensions["local_store"].close()


def test_window_bridge_does_not_expose_internal_objects_or_keys(tmp_path):
    app, client, bridge, page, csrf = make_app(tmp_path)
    try:
        bridge._store.profile = profile()
        assert all(name.startswith("_") for name in vars(bridge))
        result = bridge.connect(csrf)
        body = json.dumps(result)
        for value in (profile().sdk_key, profile().development_code, profile().session_prefix):
            assert value not in body
        assert not bridge.save_image("data:image/jpeg;base64,AAAA", "report.jpg", "x")["ok"]
    finally:
        app.extensions["local_store"].close()


@pytest.mark.parametrize("invalid", [False, True])
def test_ready_resources_show_missing_or_invalid_scan_profile(tmp_path, invalid):
    if invalid:
        (tmp_path / "game-protocol.json").write_text("synthetic-invalid", encoding="utf-8")
    app, client, bridge, page, csrf = make_app(tmp_path)
    bridge._resources.current = tmp_path / "resources/current"
    try:
        assert bridge.status()["resources"]["ready"]
        expected = "LOCAL_PROFILE_INVALID" if invalid else "LOCAL_PROFILE_REQUIRED"
        assert bridge.status()["game_error"] == expected
        state = client.get("/api/state").json
        assert not state["qr_available"]
        assert state["qr_error"] == expected
        assert state["can_import"]
        response = client.post(
            "/api/start", json={"server": "ios", "consent": True},
            headers={"Origin": bridge._config.origin, "X-FGO-CSRF": csrf},
        )
        assert response.status_code == 409
        assert response.json == {"error": expected}
    finally:
        app.extensions["local_store"].close()


@pytest.mark.parametrize("region,prefix", [("CN", "fgo-cn-ios"), ("JP", "fgo-jp")])
def test_json_saved_only_after_explicit_local_save_dialog(tmp_path, region, prefix):
    app, client, bridge, page, csrf = make_app(tmp_path)
    try:
        bridge.connect(csrf)
        session = bridge._session
        session.export = b'{"synthetic_statistics":true}'
        session.phase = "complete"
        session.server = "ios"
        session.preview = {"region": region}
        session.expires = time.monotonic() + 600
        target = tmp_path / "export.json"

        class Window:
            def create_file_dialog(self, *args, **kwargs):
                assert kwargs["save_filename"].startswith(prefix + "-statistics-")
                return str(target)

        bridge._window = Window()
        assert bridge.save_json()["saved"]
        assert target.read_bytes() == session.export
        assert bridge.save_json()["error"] == "EXPORT_PATH_EXISTS"
        session.clear()
        assert bridge.save_json()["error"] == "STATISTICS_NOT_READY"
    finally:
        app.extensions["local_store"].close()


@pytest.mark.parametrize(
    "filename,accepted",
    [
        ("fgo-cn-ios-report.jpg", True),
        ("fgo-cn-android-report-01.jpg", True),
        ("fgo-jp-report.jpg", True),
        ("fgo-jp-report-02.jpg", True),
        ("../fgo-jp-report.jpg", False),
        ("fgo-jp-report.png", False),
        ("fgo-jp-report-001.jpg", False),
    ],
)
def test_report_save_validates_image_and_name_before_opening_dialog(tmp_path, filename, accepted):
    app, client, bridge, page, csrf = make_app(tmp_path)
    opened = []
    target = tmp_path / "saved-report.jpg"

    class Window:
        def create_file_dialog(self, *args, **kwargs):
            opened.append(kwargs["save_filename"])
            return str(target)

    try:
        bridge.connect(csrf)
        bridge._window = Window()
        session = bridge._session
        session.phase = "complete"
        session.expires = time.monotonic() + 600
        session.document = SimpleNamespace(image_warning="", revision="synthetic", byte_size=0)
        session.report_version = "current"
        output = io.BytesIO()
        Image.new("RGB", (1200, 800), "#e4f3f7").save(output, "JPEG")
        raw = output.getvalue()
        value = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        result = bridge.save_image(value, filename, "current")
        if accepted:
            assert result["saved"] and target.read_bytes() == raw
            assert len(opened) == 1 and opened[0].startswith(filename[:-4] + "-")
            assert bridge.save_image(value, filename, "old")["error"] == "REPORT_EXPIRED"
            assert len(opened) == 1
        else:
            assert result["error"] == "IMAGE_DATA_INVALID"
            assert opened == [] and not target.exists()
    finally:
        app.extensions["local_store"].close()


def test_dictionary_folder_is_visible_and_opened_without_accepting_a_path(tmp_path, monkeypatch):
    app, client, bridge, page, csrf = make_app(tmp_path)
    root = tmp_path / "public-resources"
    (root / "data").mkdir(parents=True)
    bridge._resources.current = root
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    try:
        bridge.connect(csrf)
        assert bridge.status()["dictionary_directory"] == str(root / "data")
        assert bridge.open_dictionary_folder()["ok"]
        assert opened == [str(root / "data")]
    finally:
        app.extensions["local_store"].close()


def test_changing_data_directory_waits_for_restart_and_preserves_current_files(tmp_path):
    app, client, bridge, page, csrf = make_app(tmp_path)
    original = tmp_path / "keep.txt"
    original.write_text("existing data")
    selected = tmp_path / "new-data"
    bridge._storage = MemorySettings()

    class Window:
        result = (str(selected),)

        def create_file_dialog(self, *_args, **_kwargs):
            return self.result

    bridge._window = Window()
    try:
        bridge.connect(csrf)
        result = bridge.choose_data_directory()
        assert result["ok"] and result["restart_required"]
        assert bridge._storage.saved == [selected]
        assert bridge.status()["data_directory"] == str(tmp_path)
        assert bridge.status()["pending_data_directory"] == str(selected)
        assert bridge._resources.root == tmp_path / "resources"
        assert original.read_text() == "existing data"
        bridge._window.result = None
        assert bridge.choose_data_directory() == {"ok": True, "changed": False}
        assert bridge.status()["pending_data_directory"] == str(selected)
        bridge._window.result = (str(tmp_path),)
        assert not bridge.choose_data_directory()["restart_required"]
        assert bridge.status()["pending_data_directory"] == ""
    finally:
        app.extensions["local_store"].close()


def test_unconfigured_page_waits_for_selection_and_cancel_keeps_it_open(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData"
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    app, client, bridge, page, csrf = make_app(tmp_path, configured=False)
    bridge._storage = MemorySettings()

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return None

    bridge._window = Window()
    try:
        assert bridge.connect(csrf)["data_directory"] == ""
        assert bridge.status()["resources"] == {"phase": "waiting", "ready": False}
        for action in (bridge.prepare_resources, bridge.choose_resources, bridge.open_data_folder):
            assert action()["error"] == "DATA_DIRECTORY_REQUIRED"
        assert bridge.choose_data_directory() == {"ok": True, "changed": False}
        assert bridge._resources is None and bridge._storage.saved == []
        assert client.get("/").status_code == 200
        assert not appdata.exists() and list(tmp_path.iterdir()) == []
    finally:
        app.extensions["local_store"].close()


def test_first_manual_selection_prepares_chosen_directory_without_restart(tmp_path, monkeypatch):
    app, client, bridge, page, csrf = make_app(tmp_path, configured=False)
    selected = tmp_path / "自选数据目录"
    bridge._storage = MemorySettings()
    prepared = []
    monkeypatch.setattr(
        ResourceManager, "prepare", lambda manager, callback: prepared.append(manager.root)
    )

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return (str(selected),)

    bridge._window = Window()
    try:
        bridge.connect(csrf)
        assert prepared == []
        result = bridge.choose_data_directory()
        assert result == {
            "ok": True, "changed": True, "restart_required": False, "first_selection": True
        }
        assert prepared == [selected / "resources"]
        assert bridge._storage.saved == [selected]
        assert bridge.status()["data_directory"] == str(selected)
        assert bridge.status()["pending_data_directory"] == ""
        assert read_startup_directory(settings=bridge._storage) == selected
        assert not bridge.status()["selecting_data_directory"]
    finally:
        app.extensions["local_store"].close()


def test_failed_manual_settings_write_keeps_page_unconfigured(tmp_path):
    app, client, bridge, page, csrf = make_app(tmp_path, configured=False)

    class FailedSettings(MemorySettings):
        def save(self, directory):
            raise ExportError("DATA_SETTINGS_UNAVAILABLE")

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return (str(tmp_path / "chosen"),)

    bridge._storage, bridge._window = FailedSettings(), Window()
    try:
        assert bridge.choose_data_directory()["error"] == "DATA_SETTINGS_UNAVAILABLE"
        assert bridge.status()["data_directory"] == "" and bridge._resources is None
        assert not (tmp_path / "chosen/resources").exists()
        assert client.get("/").status_code == 200
    finally:
        app.extensions["local_store"].close()


def test_duplicate_manual_selection_does_not_open_another_dialog(tmp_path):
    app, client, bridge, page, csrf = make_app(tmp_path, configured=False)
    opened, release = threading.Event(), threading.Event()

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            opened.set()
            assert release.wait(3)
            return None

    bridge._window = Window()
    worker = threading.Thread(target=bridge.choose_data_directory)
    try:
        worker.start()
        assert opened.wait(3)
        assert bridge.status()["selecting_data_directory"]
        assert bridge.choose_data_directory()["error"] == "DATA_DIRECTORY_BUSY"
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        app.extensions["local_store"].close()


def test_first_start_opens_window_without_selecting_or_preparing_resources(tmp_path, monkeypatch):
    import webview

    from fgo_local_exporter.desktop import main

    monkeypatch.setattr(sys, "argv", ["FGOStatDesktop.exe"])
    monkeypatch.setattr("fgo_local_exporter.desktop.StorageSettings", MemorySettings)
    monkeypatch.setattr(
        "fgo_local_exporter.game_profile.default_profile_path",
        lambda: tmp_path / "game-protocol.json",
    )
    monkeypatch.setattr("fgo_local_exporter.desktop_storage.tempfile.tempdir", str(tmp_path))
    calls, browser_paths = [], []

    def create_window(title, url, *, js_api, **_kwargs):
        calls.append("page")
        assert js_api._resources is None
        return object()

    def start(**kwargs):
        from pathlib import Path

        calls.append("browser")
        path = Path(kwargs["storage_path"])
        assert path.is_dir()
        browser_paths.append(path)

    def unexpected(*_args, **_kwargs):
        pytest.fail("首次进入页面前不能准备或下载资源")

    monkeypatch.setattr(webview, "create_window", create_window)
    monkeypatch.setattr(webview, "start", start)
    monkeypatch.setattr(ResourceManager, "__init__", unexpected)
    main()
    assert calls == ["page", "browser"]
    assert len(browser_paths) == 1 and not browser_paths[0].exists()


@pytest.mark.parametrize(
    "filename,url",
    [
        ("../escape.json", "https://api.atlasacademy.io/repo/CN/master/mstShop.json"),
        ("safe.json", "https://unapproved.invalid/api/export"),
        ("safe.json", "https://127.0.0.1/private.json"),
        ("safe.json", "https://api.atlasacademy.io/repo/CN/file.json?token=synthetic"),
    ],
)
def test_resource_recipe_rejects_paths_and_unapproved_destinations(tmp_path, filename, url):
    manager = ResourceManager(tmp_path)
    with pytest.raises(ExportError, match="RESOURCE_MANIFEST_INVALID"):
        manager._file(tmp_path, {"filename": filename, "url": url, "bytes": 2, "sha256": "0" * 64})

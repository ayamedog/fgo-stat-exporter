import importlib.util
import json
import sys
import zipfile
from pathlib import Path


def _load_extractor():
    script = Path(__file__).parents[1] / "tools" / "extract_game_protocol.py"
    spec = importlib.util.spec_from_file_location("extract_game_protocol", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_extracts_only_validated_app_protocol_from_apk(tmp_path, monkeypatch, capsys):
    profile = {
        "version": "2.129.0",
        "sdk_base": {
            "merchant_id": "1",
            "game_id": "112",
            "app_id": "112",
            "server_id": "248",
            "version": "3",
            "sdk_type": "1",
        },
        "sdk_key": "SyntheticSDKKey0123456789012345",
        "development_code": "SyntheticDevelopmentCode0123456789",
        "session_prefix": "SyntheticSessionPrefix0123456789",
    }
    apk = tmp_path / "sample.apk"
    output = tmp_path / "game-protocol.json"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("assets/game-protocol.json", json.dumps(profile))

    monkeypatch.setattr(
        sys,
        "argv",
        ["extract_game_protocol.py", "--apk", str(apk), "--output", str(output)],
    )
    _load_extractor().main()

    assert json.loads(output.read_text(encoding="utf-8")) == profile
    assert "SyntheticSDKKey" not in capsys.readouterr().out


def test_rejects_apk_without_protocol_asset(tmp_path, monkeypatch):
    apk = tmp_path / "empty.apk"
    output = tmp_path / "game-protocol.json"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("assets/other.json", "{}")
    monkeypatch.setattr(
        sys,
        "argv",
        ["extract_game_protocol.py", "--apk", str(apk), "--output", str(output)],
    )

    try:
        _load_extractor().main()
    except SystemExit as error:
        assert str(error) == "APK_PROTOCOL_NOT_FOUND"
    else:
        raise AssertionError("APK without game-protocol.json must be rejected")
    assert not output.exists()

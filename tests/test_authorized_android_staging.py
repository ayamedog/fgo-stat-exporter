"""人工验证本次 Android 页面源码授权不扩大到其他发布内容。"""

import hashlib
import importlib
import json
import marshal
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    return importlib.import_module("prepare_android")


def test_compiled_module_has_no_source_paths_or_line_tables(builder):
    source = b'def outer():\n    def inner():\n        return "artificial"\n    return inner()\n'
    pyc = builder.compile_release(source)
    code = marshal.loads(pyc[16:])

    def check(item):
        assert item.co_filename == "<release>"
        assert not item.co_linetable
        assert item.co_firstlineno == 1
        for value in item.co_consts:
            if isinstance(value, types.CodeType):
                check(value)

    check(code)
    namespace = {}
    exec(code, namespace)
    assert namespace["outer"]() == "artificial"
    assert source not in pyc


def test_overlapping_work_rejected_before_writing(tmp_path):
    root = Path(__file__).parents[1]
    target = tmp_path / "output"
    reviewed = tmp_path / "resources.json"
    reviewed.write_bytes(b"{}")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(root / "tools/prepare_android.py"),
         "--authorized-frontend-for-this-release", "--output", str(target),
         "--work", str(target / "inside"), "--manifest-output", str(tmp_path / "manifest.json"),
         "--resource-review", str(reviewed),
         "--resource-review-sha256", hashlib.sha256(b"{}").hexdigest()],
        capture_output=True, timeout=30, cwd=root,
    )
    assert result.returncode != 0
    assert "相互隔离" in result.stderr.decode("utf-8", errors="replace")
    assert not target.exists()


def test_shared_revision_rejects_unpackaged_python(builder, monkeypatch, tmp_path):
    root = tmp_path / "project"
    core = root / "src/fgo_stat_core"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "CORE_MODULES", ("__init__.py",))
    monkeypatch.setattr(builder, "CORE_DATA", ())
    monkeypatch.setattr(builder, "shared_report_revision", lambda _root: "0123456789abcdef")
    assert builder.checked_report_revision() == "0123456789abcdef"
    (core / "extra.py").write_text("# artificial")
    with pytest.raises(RuntimeError, match="共享报告源码"):
        builder.checked_report_revision()


def test_resource_proposal_is_complete_and_does_not_create_archive(builder, monkeypatch, tmp_path):
    source = tmp_path / "source-public"
    source.mkdir()
    work = tmp_path / "work"
    manifest = tmp_path / "review.json"

    def fake_copy(_source, destination, _core):
        (destination / "data").mkdir(parents=True)
        (destination / "data/example.json").write_bytes(b"{}")
        return {"data/example.json": hashlib.sha256(b"{}").hexdigest()}

    monkeypatch.setattr(builder, "copy_resources", fake_copy)
    monkeypatch.setattr(builder, "check_required_fonts_and_native_resources", lambda _public: None)
    builder.propose_resource_review(source, work, manifest)
    assert json.loads(manifest.read_bytes()) == {
        "data/example.json": hashlib.sha256(b"{}").hexdigest()
    }
    assert not list(work.rglob("*.zip"))


def test_resource_proposal_rejects_unlisted_file_before_manifest(builder, monkeypatch, tmp_path):
    source = tmp_path / "source-public"
    source.mkdir()
    work = tmp_path / "work"
    manifest = tmp_path / "review.json"

    def bad_copy(_source, destination, _core):
        destination.mkdir(parents=True)
        (destination / "unexpected").write_bytes(b"unreviewed")
        return {}

    monkeypatch.setattr(builder, "copy_resources", bad_copy)
    monkeypatch.setattr(builder, "check_required_fonts_and_native_resources", lambda _public: None)
    with pytest.raises(RuntimeError, match="完整范围不符"):
        builder.propose_resource_review(source, work, manifest)
    assert not manifest.exists()


def test_resource_proposal_rejects_source_even_if_listed(builder, monkeypatch, tmp_path):
    source = tmp_path / "source-public"
    source.mkdir()
    work = tmp_path / "work"
    manifest = tmp_path / "review.json"

    def bad_copy(_source, destination, _core):
        (destination / "data").mkdir(parents=True)
        (destination / "data/source.py").write_text("# artificial")
        return {"data/source.py": hashlib.sha256(b"# artificial").hexdigest()}

    monkeypatch.setattr(builder, "copy_resources", bad_copy)
    monkeypatch.setattr(builder, "check_required_fonts_and_native_resources", lambda _public: None)
    with pytest.raises(RuntimeError, match="未授权文件"):
        builder.propose_resource_review(source, work, manifest)
    assert not manifest.exists()


def test_authorized_fonts_and_native_assets_cannot_be_omitted(builder, monkeypatch, tmp_path):
    core = tmp_path / "src/fgo_stat_core/infrastructure/native_support_ui"
    core.mkdir(parents=True)
    (core / "asset_hashes.json").write_text(json.dumps({"icons/example.png": "1" * 64}))
    (core / "font_manifest.json").write_text(json.dumps({
        "key": "font/example.otf", "sha256": "2" * 64,
    }))
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    public = {
        "native-ui/icons/example.png": "1" * 64,
        "native-ui/font/example.otf": "2" * 64,
        "fonts/NotoSansCJK-Regular.ttc":
            "d18a3adba5c891eb7f9cdf8c31dad28dcc324db80e662d6a0f91e8db6aee5913",
        "fonts/DejaVuSans.ttf":
            "f9a7bd58cdaa9d1e9acafd070db8dea0b6db9cf2b540501b15075315a47cba3d",
    }
    builder.check_required_fonts_and_native_resources(public)
    del public["native-ui/icons/example.png"]
    with pytest.raises(RuntimeError, match="原生素材或字体缺失"):
        builder.check_required_fonts_and_native_resources(public)


@pytest.fixture
def prepared(tmp_path, monkeypatch, builder):
    root = tmp_path / "source"
    stage = tmp_path / "stage"
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "_source_files", lambda: ["demo/runtime.py"])
    monkeypatch.setattr(builder, "_data_files", lambda: ["demo/page.js"])
    monkeypatch.setattr(builder, "checked_report_revision", lambda: "0123456789abcdef")
    monkeypatch.setattr(builder, "LICENSE_FIXED_SHA256", {})
    monkeypatch.setattr(builder, "check_required_fonts_and_native_resources", lambda _public: None)
    (root / "src/demo").mkdir(parents=True)
    (root / "src/demo/runtime.py").write_bytes(b"VALUE = 42\n")
    (root / "src/demo/page.js").write_bytes(b"// artificial authorised page\n")
    (stage / "python/demo").mkdir(parents=True)
    (stage / "python/demo/runtime.pyc").write_bytes(builder.compile_release(b"VALUE = 42\n"))
    (stage / "python/demo/page.js").write_bytes(b"// artificial authorised page\n")
    (stage / "assets/licenses").mkdir(parents=True)
    for source, target in builder.LICENSES:
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-license")
        (stage / "assets/licenses" / target).write_text("synthetic-license")
    config = {
        "version": "2.129.0",
        "sdk_base": {"merchant_id": "80001", "game_id": "80002", "app_id": "80003",
                     "server_id": "80004", "version": "80005", "sdk_type": "80006"},
        "sdk_key": "SyntheticSDKKey1234567890",
        "development_code": "SyntheticDevelopment123456",
        "session_prefix": "SyntheticSessionPrefix123456",
    }
    (root / "game-protocol.json").write_text(json.dumps(config))
    (stage / "assets/game-protocol.json").write_text(json.dumps(config))
    (stage / "assets/report-engine.json").write_text(
        json.dumps({"version": 1, "revision": "0123456789abcdef"})
    )
    public_data = b'{}'
    public = {"data/artificial.json": hashlib.sha256(public_data).hexdigest()}
    (stage / "assets/public-manifest.json").write_text(json.dumps(public))
    resource_review = tmp_path / "reviewed-public.json"
    resource_review.write_text(json.dumps(public))
    resource_sha = hashlib.sha256(resource_review.read_bytes()).hexdigest()
    with zipfile.ZipFile(stage / "assets/public-resources.zip", "w") as archive:
        archive.writestr("data/artificial.json", public_data)

    def freeze():
        raw = json.dumps(builder.stage_inventory(stage)).encode()
        manifest = tmp_path / "independent-manifest.json"
        manifest.write_bytes(raw)
        return manifest, hashlib.sha256(raw).hexdigest(), resource_review, resource_sha

    return stage, freeze


def test_validated_exact_prepared_set(builder, prepared):
    stage, freeze = prepared
    builder.verify_inventory(stage, *freeze())


@pytest.mark.parametrize("extra", ["other.py", "other.js", "hidden/source.map", "empty/"])
def test_unapproved_file_or_directory_rejected_even_with_new_manifest(builder, prepared, extra):
    stage, freeze = prepared
    path = stage / extra
    if extra.endswith("/"):
        path.mkdir(parents=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    with pytest.raises(RuntimeError, match="暂存范围或载荷"):
        builder.verify_inventory(stage, *freeze())


def test_authorized_page_changed_rejected_even_with_new_manifest(builder, prepared):
    stage, freeze = prepared
    (stage / "python/demo/page.js").write_text("changed")
    with pytest.raises(RuntimeError, match="运行资源"):
        builder.verify_inventory(stage, *freeze())


def test_compiled_module_changed_rejected_even_with_new_manifest(builder, prepared):
    stage, freeze = prepared
    (stage / "python/demo/runtime.pyc").write_bytes(builder.compile_release(b"VALUE = 99\n"))
    with pytest.raises(RuntimeError, match="编译模块"):
        builder.verify_inventory(stage, *freeze())


def test_other_valid_fixed_config_rejected_even_with_new_manifest(builder, prepared):
    stage, freeze = prepared
    path = stage / "assets/game-protocol.json"
    config = json.loads(path.read_text())
    config["sdk_key"] = "AnotherSyntheticKey123456"
    path.write_text(json.dumps(config))
    with pytest.raises(RuntimeError, match="应用参数"):
        builder.verify_inventory(stage, *freeze())


def test_engine_mismatch_rejected_even_with_new_manifest(builder, prepared):
    stage, freeze = prepared
    (stage / "assets/report-engine.json").write_text(
        json.dumps({"version": 1, "revision": "ffffffffffffffff"})
    )
    with pytest.raises(RuntimeError, match="报告引擎指纹"):
        builder.verify_inventory(stage, *freeze())


def test_changed_public_resources_rejected_even_with_new_stage_manifest(builder, prepared):
    stage, freeze = prepared
    content = b'{"changed":true}'
    (stage / "assets/public-manifest.json").write_text(json.dumps({
        "data/artificial.json": hashlib.sha256(content).hexdigest(),
    }))
    with zipfile.ZipFile(stage / "assets/public-resources.zip", "w") as archive:
        archive.writestr("data/artificial.json", content)
    with pytest.raises(RuntimeError, match="资源与独立审核清单"):
        builder.verify_inventory(stage, *freeze())


@pytest.mark.parametrize("injected", ["stage", "work"])
def test_unreviewed_file_prevents_nested_archive(
    builder, prepared, tmp_path, monkeypatch, injected
):
    _stage, freeze = prepared
    _manifest, _digest, resource_review, resource_sha = freeze()
    output, work = tmp_path / "new-stage", tmp_path / "new-work"
    public_data = b"{}"

    def copy_with_extra(_source, destination, _core):
        (destination / "data").mkdir(parents=True)
        (destination / "data/artificial.json").write_bytes(public_data)
        extra = output / "unexpected.txt" if injected == "stage" else work / "unexpected.txt"
        extra.write_text("not approved")
        return {"data/artificial.json": hashlib.sha256(public_data).hexdigest()}

    monkeypatch.setattr(builder, "prepared_resources", lambda _path: tmp_path)
    monkeypatch.setattr(builder, "copy_resources", copy_with_extra)
    monkeypatch.setattr(sys, "argv", [
        "prepare_android.py", "--authorized-frontend-for-this-release", "--output", str(output),
        "--work", str(work), "--manifest-output", str(tmp_path / "new-manifest.json"),
        "--resource-review", str(resource_review), "--resource-review-sha256", resource_sha,
    ])
    with pytest.raises(RuntimeError, match="归档前安卓暂存范围|资源工作目录"):
        builder.main()
    assert not (output / "assets/public-resources.zip").exists()

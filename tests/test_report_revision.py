import hashlib
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fgo_local_exporter.desktop_reports import DesktopReportBuilder
from fgo_local_exporter.errors import ExportError
from fgo_local_exporter.mobile import MobileRuntime, _packaged_engine_revision
from fgo_local_exporter.reports import ReportBuilder, shared_report_revision
from fgo_local_exporter.settings import LocalConfig


@pytest.mark.parametrize(
    "name",
    [
        "features/report/calculator.py",
        "features/capture_import/parser.py",
        "infrastructure/report_ui/template.html",
        "infrastructure/native_support_ui/typography.py",
        "infrastructure/native_support_ui/asset_hashes.json",
        "contracts/report.py",
    ],
)
def test_shared_module_changes_require_restart(tmp_path, name):
    config = make_config(tmp_path, name)
    source = config.engine_source
    root = source / "fgo_stat_core"
    builder = ReportBuilder(config)
    before = shared_report_revision(source)
    (root / name).write_text("synthetic version B", encoding="utf-8")
    assert shared_report_revision(source) != before
    with pytest.raises(ExportError, match="REPORT_ENGINE_RESTART_REQUIRED"):
        builder.render(b"synthetic", {"friend": False, "support": False}, threading.Event())


def make_config(tmp_path, name):
    source = tmp_path / "src"
    root = source / "fgo_stat_core"
    for filename in (
        "infrastructure/report_ui/builder.py",
        "infrastructure/native_support_render.py",
        "infrastructure/native_support_adapter.py",
        "infrastructure/report_master.py",
        "infrastructure/report_assets.py",
        "infrastructure/report_fonts.py",
        name,
    ):
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic version A", encoding="utf-8")
    for folder in ("data", "atlas"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "SOURCE_MANIFEST.json").write_text('{"files":[]}')
    return LocalConfig(
        engine_source=source,
        data_dir=tmp_path / "data",
        atlas_dir=tmp_path / "atlas",
        asset_dir=tmp_path / "assets",
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.invalid",
        "https://public.invalid/path",
        "https://name:password@public.invalid",
        "https://127.0.0.1:55313",
    ],
)
def test_rejects_remote_or_ambiguous_origins(origin):
    with pytest.raises(ExportError, match="LOCAL_CONFIG_INVALID"):
        LocalConfig(origin=origin)


def test_resource_manifest_update_also_invalidates_cached_calculations(tmp_path):
    config = make_config(tmp_path, "features/report/calculator.py")
    builder = ReportBuilder(config)
    before = builder.revision()
    (config.data_dir / "SOURCE_MANIFEST.json").write_text('{"files":[],"revision":"B"}')
    assert builder.revision() != before
    with pytest.raises(ExportError, match="REPORT_ENGINE_RESTART_REQUIRED"):
        builder.render(b"synthetic", {"friend": False, "support": False}, threading.Event())


def test_packaged_revision_matches_source_even_when_only_bytecode_is_present(tmp_path):
    config = make_config(tmp_path, "features/report/calculator.py")
    source_revision = shared_report_revision(config.engine_source)
    # 单独的人工目录模拟 APK 仅包含编译模块和许可内的资源。
    packaged_source = tmp_path / "packaged"
    root = packaged_source / "fgo_stat_core/features/report"
    root.mkdir(parents=True)
    (root / "calculator.pyc").write_bytes(b"synthetic-bytecode")
    config = replace(config, engine_source=packaged_source)
    manifest = f'{{"version":1,"revision":"{source_revision}"}}'
    revision = _packaged_engine_revision(manifest)
    builder = DesktopReportBuilder(config, packaged_revision=revision)
    expected = hashlib.sha256()
    expected.update(source_revision.encode())
    expected.update((config.data_dir / "SOURCE_MANIFEST.json").read_bytes())
    expected.update((config.atlas_dir / "SOURCE_MANIFEST.json").read_bytes())
    assert builder.revision() == expected.hexdigest()[:16]
    (config.data_dir / "SOURCE_MANIFEST.json").write_text('{"files":["synthetic"]}')
    assert builder.revision() != expected.hexdigest()[:16]


@pytest.mark.parametrize("raw", [
    "{}", '{"version":true,"revision":"0123456789abcdef"}',
    '{"version":1,"revision":"0123456789abcdef","extra":true}',
    '{"version":1,"revision":"0123456789abcdef","revision":"0000000000000000"}',
    '{"version":1,"revision":"INVALID"}', "a" * 1025,
])
def test_invalid_packaged_revision_is_rejected(raw):
    with pytest.raises(ExportError, match="REPORT_NOT_CONFIGURED"):
        _packaged_engine_revision(raw)


def test_no_packaged_manifest_keeps_development_revision():
    assert _packaged_engine_revision(None) is None


def test_builder_rejects_invalid_packaged_revision(tmp_path):
    config = make_config(tmp_path, "features/report/calculator.py")
    with pytest.raises(ExportError, match="REPORT_NOT_CONFIGURED"):
        DesktopReportBuilder(config, packaged_revision="not-a-revision")


def test_mobile_resources_use_packaged_revision_when_rebuilding_builder(tmp_path):
    config = make_config(tmp_path, "features/report/calculator.py")
    revision = shared_report_revision(config.engine_source)
    root = tmp_path / "mobile-resources"
    for name in ("data", "atlas-cn"):
        (root / name).mkdir(parents=True)
        (root / name / "SOURCE_MANIFEST.json").write_text('{"files":[]}')
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    class Resources:
        def image_cache(self, _bundle, _root):
            return tmp_path / "image-cache"

        def describe_images(self, _cache, _native):
            pass

    runtime = MobileRuntime.__new__(MobileRuntime)
    runtime._config = config
    runtime._bundle = bundle
    runtime._resources = Resources()
    runtime._store = SimpleNamespace(lock=threading.RLock(), builder=None)
    runtime._packaged_revision = revision
    runtime._attach_resources(root)
    before = runtime._store.builder.revision()
    runtime._attach_resources(root)
    assert runtime._store.builder.revision() == before
    expected = hashlib.sha256()
    expected.update(revision.encode())
    expected.update((root / "data/SOURCE_MANIFEST.json").read_bytes())
    expected.update((root / "atlas-cn/SOURCE_MANIFEST.json").read_bytes())
    assert before == expected.hexdigest()[:16]

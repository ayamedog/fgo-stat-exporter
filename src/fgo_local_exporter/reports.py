"""装配独立统计引擎、报告模板和助战组件；完整图片由本机浏览器引擎生成。"""

import asyncio
import hashlib
import importlib.util
import json
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fgo_local_exporter.errors import ExportError

__all__ = ["ReportBuilder", "ReportDocument", "shared_report_revision"]


def shared_report_revision(source):
    root = source / "fgo_stat_core"
    if not root.is_dir():
        spec = importlib.util.find_spec("fgo_stat_core")
        if spec is None or spec.origin is None:
            raise ExportError("REPORT_NOT_CONFIGURED")
        root = Path(spec.origin).parent
    files = {
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".html", ".json"}
    }
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


@dataclass(repr=False)
class ReportDocument:
    html: str
    assets: dict[str, bytes] = field(repr=False)
    data: dict = field(repr=False)
    revision: str
    image_warning: str = ""

    @property
    def byte_size(self):
        return (
            len(self.html.encode())
            + sum(map(len, self.assets.values()))
            + len(json.dumps(self.data, ensure_ascii=False).encode())
        )


class ReportBuilder:
    """只调用共享报告模块，不包含独立的公式或报告模板副本。"""

    def __init__(self, config):
        self.config = config
        for name in ("engine_source", "data_dir", "atlas_dir", "asset_dir"):
            if getattr(config, name) is None:
                raise ExportError("REPORT_NOT_CONFIGURED")
        root = config.engine_source.resolve()
        if (root / "fgo_stat_core/infrastructure/report_ui/builder.py").is_file() and str(
            root
        ) not in sys.path:
            sys.path.insert(0, str(root))
        if importlib.util.find_spec("fgo_stat_core") is None:
            raise ExportError("REPORT_NOT_CONFIGURED")
        self._master = {}
        self._master_lock = threading.Lock()
        self._revision = self.revision()

    def revision(self):
        digest = hashlib.sha256()
        digest.update(shared_report_revision(self.config.engine_source).encode())
        for directory in (self.config.data_dir, self.config.atlas_dir, self.config.atlas_jp_dir):
            if directory is None:
                continue
            digest.update((directory / "SOURCE_MANIFEST.json").read_bytes())
        return digest.hexdigest()[:16]

    def master(self, region="CN"):
        from fgo_stat_core.infrastructure.report_master import load_report_master

        with self._master_lock:
            if region not in self._master:
                directory = self.config.atlas_jp_dir if region == "JP" else self.config.atlas_dir
                if directory is None:
                    raise ExportError("REPORT_REGION_NOT_CONFIGURED")
                self._master[region] = load_report_master(
                    self.config.data_dir, directory, region=region
                )
            return self._master[region]

    def gacha_history(self, raw):
        from fgo_stat_core.features.capture_import.parser import parse_capture
        from fgo_stat_core.features.report.gacha_records import build_gacha_history

        try:
            if self.revision() != self._revision:
                raise ExportError("REPORT_ENGINE_RESTART_REQUIRED")
            snapshot = parse_capture(
                raw, secrets.token_bytes(32), report_fields=True, allow_jp=True
            )
            return build_gacha_history(snapshot, self.master(snapshot.region))
        except ExportError:
            raise
        except Exception:
            raise ExportError("DETAILS_DATA_INVALID") from None

    def gacha_image(self, details, gacha_id):
        from fgo_stat_core.infrastructure.public_download import PublicImageDownloader
        from fgo_stat_core.infrastructure.report_assets import AssetSpec, fetch_report_assets

        record = next((row for row in details.records if row.gacha_id == gacha_id), None)
        if record is None or record.image_id is None:
            raise ExportError("DETAILS_IMAGE_UNAVAILABLE")
        prefix = "jp-" if details.region == "JP" else ""
        name = f"banner-{prefix}image-{record.image_id}.png"
        spec = AssetSpec(
            name,
            f"https://static.atlasacademy.io/{details.region}/SummonBanners/"
            f"img_summon_{record.image_id}.png",
        )

        async def prepare():
            async with asyncio.timeout(15), PublicImageDownloader(timeout=12) as source:
                await fetch_report_assets((spec,), self.config.asset_dir, source, repair_cache=True)

        try:
            asyncio.run(prepare())
            path = self.config.asset_dir / name
            if path.is_symlink() or not path.is_file():
                raise ExportError("DETAILS_IMAGE_UNAVAILABLE")
            return path.read_bytes()
        except Exception:
            raise ExportError("DETAILS_IMAGE_UNAVAILABLE") from None

    def render(self, raw, options, cancelled, *, progress=None):
        def checkpoint(stage, completed=0, total=0):
            if cancelled.is_set():
                raise ExportError("CANCELLED")
            if progress is not None:
                progress(stage, completed, total)

        try:
            checkpoint("parsing")
            if self.revision() != self._revision:
                raise ExportError("REPORT_ENGINE_RESTART_REQUIRED")
            from fgo_stat_core.features.capture_import.parser import parse_capture
            from fgo_stat_core.features.report.calculator import build_report
            from fgo_stat_core.infrastructure.report_assets import (
                plan_report_assets,
            )
            from fgo_stat_core.infrastructure.report_ui.builder import build

            if cancelled.is_set():
                raise ExportError("CANCELLED")
            snapshot = parse_capture(
                raw, secrets.token_bytes(32), report_fields=True, allow_jp=True
            )
            master = self.master(snapshot.region)
            checkpoint("calculating")
            report = build_report(
                snapshot,
                master,
                generated_at=int(time.time()),
                show_friend_code=options["friend"],
                show_support=options["support"],
            )
            specs = plan_report_assets(
                report, self.config.data_dir, native_support=options["support"]
            )
            image_counts = asyncio.run(
                _prepare_images(specs, self.config.asset_dir, cancelled, checkpoint)
            )
            support_html, native_assets = None, {}
            native_ready = _native_images_ready(report, self.config.asset_dir)
            missing_images = bool(image_counts["missing"] or not native_ready)
            if options["support"] and self.config.native_ui_dir is not None and native_ready:
                from fgo_stat_core.infrastructure.native_support_render import render_native_blocks

                support_html, native_assets = render_native_blocks(
                    report,
                    self.config.asset_dir,
                    self.config.native_ui_dir,
                    **({"header_font": self.config.header_font} if self.config.header_font else {}),
                    **(
                        {"regular_font": self.config.regular_font}
                        if self.config.regular_font
                        else {}
                    ),
                    progress=lambda completed, total: checkpoint("support", completed, total),
                    cancelled=cancelled.is_set,
                )
            checkpoint("layout")
            data = report.model_dump(mode="json")
            source = build(data, asset_dir=self.config.asset_dir, support_html=support_html)
            # 只调整固定标题，角色名和计算结果保持统计引擎的输出。
            # fmt: off
            labels = {
                '<title>FGO 御主纪念册</title>':
                    '<title>FGO 抓包统计</title>',
                '<h1>御主纪念册</h1>':
                    '<h1>FGO 抓包统计</h1>',
                '<div class="memory-label">CHALDEA / MEMORY FILE</div>':
                    '<div class="memory-label">FGO / CN ACCOUNT STATS</div>',
                '<p class="subtitle">把每一次相遇，收进这一本纪念册。</p>':
                    '<p class="subtitle">基于当前账号数据生成的统计快照。</p>',
                '<span>CHALDEA ARCHIVE</span>':
                    '<span>FGO CN STATS</span>',
                '<span>MEMORY RECORD</span>':
                    '<span>ACCOUNT DATA</span>',
                '<h2>相遇，都留有回响。</h2>':
                    '<h2>账号数据概览</h2>',
                '<span>MEMORY PIECE / 01</span> THE JOURNEY CONTINUES':
                    '<span>ACCOUNT / 01</span> FGO CN STATISTICS',
                '<span class="photo-tab">MY CHALDEA</span>':
                    '<span class="photo-tab">角色信息</span>',
                '<span>MY FAVORITE</span>':
                    '<span>角色展示</span>',
                '<div class="hero-empty">✦<br>待写入的回忆</div>':
                    '<div class="hero-empty">✦<br>未提供单推从者</div>',
            }
            # fmt: on

            for old, new in labels.items():
                source = source.replace(old, new)
            if snapshot.region == "JP":
                source = source.replace("FGO CN ", "FGO JP ").replace("FGO / CN ", "FGO / JP ")
            # 仅通过本机会话接口提供当前报告白名单内的图片。
            source = source.replace(
                'src="' + self.config.asset_dir.resolve().as_uri() + "/", 'src="assets/'
            )
            source, placeholders = _missing_image_tags(source, self.config.asset_dir)
            missing_images |= bool(placeholders)
            assets = {}
            for name in set(re.findall(r'(?:src|href)="assets/([a-z0-9-]+\.png)"', source)):
                path = self.config.asset_dir / name
                if path.is_symlink() or path.resolve().parent != self.config.asset_dir.resolve():
                    raise ExportError("REPORT_RESOURCE_INVALID")
                if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                    raise ExportError("REPORT_IMAGE_MISSING")
                payload = path.read_bytes()
                key = hashlib.sha256(payload).hexdigest()[:24] + ".png"
                assets[key] = payload
                source = source.replace('"assets/' + name + '"', '"ASSET_BASE/' + key + '"')
            for name, payload in native_assets.items():
                key = hashlib.sha256(payload).hexdigest()[:24] + ".png"
                assets[key] = payload
                source = source.replace('"' + name + '"', '"ASSET_BASE/' + key + '"')
            if re.search(
                r"""(?:src|href)=["'](?:file:|https?:|//|report-native:|assets/)""", source
            ):
                raise ExportError("REPORT_RESOURCE_INVALID")
            if len(assets) > 256 or sum(map(len, assets.values())) > 48 * 1024 * 1024:
                raise ExportError("REPORT_RESOURCE_LIMIT")
            if cancelled.is_set():
                raise ExportError("CANCELLED")
            return ReportDocument(
                source,
                assets,
                data,
                self._revision,
                "REPORT_IMAGES_PARTIAL" if missing_images else "",
            )
        except ExportError:
            raise
        except Exception as error:
            code = getattr(error, "code", "REPORT_BUILD_FAILED")
            allowed = {
                "REPORT_IMAGE_MISSING",
                "REPORT_DATA_INVALID",
                "MASTER_DATA_INVALID",
                "MASTER_REGION_MISMATCH",
                "REPORT_NATIVE_FONT_MISSING",
                "REPORT_FONT_CONFIG_INVALID",
                "REPORT_ASSET_CACHE_INVALID",
                "REPORT_ASSET_LIMIT",
                "REPORT_ASSET_PATH_INVALID",
            }
            raise ExportError(code if code in allowed else "REPORT_BUILD_FAILED") from None


async def _prepare_images(specs, cache, cancelled, progress, *, timeout=60):
    """整批图片共用等待上限，清除会话时终止尚未完成的下载。"""
    from fgo_stat_core.infrastructure.public_download import PublicImageDownloader
    from fgo_stat_core.infrastructure.report_assets import fetch_report_assets

    if len(specs) > 256:
        raise ExportError("REPORT_ASSET_LIMIT")
    present = sum(_cached_image(spec, cache) for spec in specs)
    completed = present
    progress("images", completed, len(specs))
    source = PublicImageDownloader(timeout=15)

    class Downloader:
        async def fetch(self, url):
            nonlocal completed
            try:
                return await source.fetch(url)
            finally:
                completed = min(completed + 1, len(specs))
                progress("images", completed, len(specs))

    async def cancellation():
        while not cancelled.is_set():
            await asyncio.sleep(0.1)

    async def prepare():
        async with source:
            return await fetch_report_assets(specs, cache, Downloader(), repair_cache=True)

    downloads = asyncio.create_task(prepare())
    cancellation_task = asyncio.create_task(cancellation())
    try:
        done, _ = await asyncio.wait(
            {downloads, cancellation_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if cancelled.is_set():
            raise ExportError("CANCELLED")
        if downloads in done:
            return await downloads
    finally:
        for task in (downloads, cancellation_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(downloads, cancellation_task, return_exceptions=True)
    # 等待上限只结束图片下载，已知统计仍可生成；后续重试补齐缺图。
    available = sum(_cached_image(spec, cache) for spec in specs)
    return {
        "available": available,
        "missing": len(specs) - available,
        "downloaded": max(0, available - present),
    }


def _cached_image(spec, cache):
    """只核验受控公开缓存；清理不完整记录，避免把残留文件当成已下载。"""
    from fgo_stat_core.contracts.models import CaptureError
    from fgo_stat_core.infrastructure.report_assets import _validate_png

    path, stamp = cache / spec.filename, cache / (spec.filename + ".sha256")
    if cache.is_symlink() or path.is_symlink() or stamp.is_symlink():
        raise ExportError("REPORT_ASSET_PATH_INVALID")
    if not path.exists() and not stamp.exists():
        return False
    try:
        if path.stat().st_size > 4 * 1024 * 1024 or stamp.stat().st_size > 256:
            raise ValueError
        raw = path.read_bytes()
        expected = (
            hashlib.sha256(spec.url.encode()).hexdigest() + ":" + hashlib.sha256(raw).hexdigest()
        )
        if stamp.read_text() != expected:
            raise ValueError
        _validate_png(raw, spec.transparent)
        return True
    except (CaptureError, OSError, UnicodeError, ValueError):
        path.unlink(missing_ok=True)
        stamp.unlink(missing_ok=True)
        return False


def _native_images_ready(report, cache):
    """缺少助战立绘时保留标准文字布局，不让一张公开图阻断整份统计。"""
    from fgo_stat_core.infrastructure.native_support_adapter import native_panels

    for panel in native_panels(report):
        for slot in panel.slots:
            if slot["state"] != "ready":
                continue
            if not (cache / f"narrow-{slot['servant_id']}.png").is_file():
                return False
            for equipment in slot["equipment"]:
                if (
                    equipment["state"] == "ready"
                    and not (cache / f"equip-{equipment['id']}.png").is_file()
                ):
                    return False
    return True


def _missing_image_tags(source, cache):
    """固定图片引用缺失时显示缺图提示，仍拒绝越界和符号链接。"""
    missing = 0

    def replace(match):
        nonlocal missing
        path = cache / match[1]
        if path.is_symlink() or path.resolve().parent != cache.resolve():
            raise ExportError("REPORT_RESOURCE_INVALID")
        if path.is_file():
            return match[0]
        missing += 1
        classes = re.search(r'\bclass="([A-Za-z0-9 _-]*)"', match[0])
        style = re.search(r'\bstyle="([^"]*)"', match[0])
        attributes = ' class="image-missing' + (" " + classes[1] if classes else "") + '"'
        if style:
            attributes += ' style="' + style[1] + '"'
        return "<span" + attributes + ' role="img" aria-label="图片未下载">缺图</span>'

    source = re.sub(r'<img\b[^>]*\bsrc="assets/([a-z0-9-]+\.png)"[^>]*>', replace, source)
    return source, missing

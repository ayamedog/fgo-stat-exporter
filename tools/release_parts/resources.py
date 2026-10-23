"""按资源清单准备字典、图片和字体，不复制运行目录。"""

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from fgo_local_exporter.desktop_resources import ResourceManager
from fgo_stat_core.contracts.report import AnnualReport
from fgo_stat_core.features.capture_import.parser import parse_capture
from fgo_stat_core.features.report.calculator import build_report
from fgo_stat_core.infrastructure.report_assets import fetch_report_assets, plan_report_assets
from fgo_stat_core.infrastructure.report_master import load_report_master


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def prepared_resources(root=None):
    if root is not None:
        return Path(root).resolve(strict=True)
    manager = ResourceManager()
    manager.restore_active()
    # 默认打包必须检查完整最新目录，不能把旧缓存直接当作发布字典。
    manager.prepare(lambda path: None, update=True)
    while manager._thread.is_alive():
        manager._thread.join(0.2)
    if not manager.state()["ready"] or manager.state()["phase"] != "ready":
        raise RuntimeError(manager.state()["error"]) from None
    result = manager.current
    # 默认构建只准备通用图标，不用真实账号规划图片集合。
    raw = json.dumps(
        {
            "response": [{"nid": "login", "resCode": "00", "success": {}}],
            "cache": {
                "serverTime": 1792500000,
                "replaced": {
                    "userGame": [
                        {
                            "userId": "910000000001",
                            "friendCode": "100101102103",
                            "rkchannel": "24",
                            "lv": 1,
                            "createdAt": 1477000000,
                        }
                    ],
                    "userSvt": [],
                    "userSvtCollection": [],
                },
            },
        }
    ).encode()
    snapshot = parse_capture(raw, b"SYNTHETIC-BUILD-ASSET-PLAN-KEY-000", report_fields=True)
    master = load_report_master(result / "data", result / "atlas-cn")
    report: AnnualReport = build_report(snapshot, master, generated_at=1792500000)
    asyncio.run(fetch_report_assets(plan_report_assets(report, result / "data"), result / "assets"))
    return result


def copy_resources(source, destination, core):
    expected = {}

    def add(relative, digest=None, size=None):
        path = source / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source):
            raise RuntimeError("资源路径不在审核范围内：" + relative)
        raw = path.read_bytes()
        if digest is not None and sha(raw) != digest:
            raise RuntimeError("资源哈希不符：" + relative)
        if size is not None and len(raw) != size:
            raise RuntimeError("资源大小不符：" + relative)
        expected[relative] = sha(raw)

    load_report_master(source / "data", source / "atlas-cn")
    load_report_master(source / "data", source / "atlas-jp", region="JP")
    for group in ("data", "atlas-cn", "atlas-jp"):
        metadata = json.loads((source / group / "SOURCE_MANIFEST.json").read_bytes())
        add(group + "/SOURCE_MANIFEST.json")
        for spec in metadata["files"]:
            name = spec["filename"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name):
                raise RuntimeError("字典文件名不合法")
            add(group + "/" + name, spec["sha256"], spec["bytes"])
    image = re.compile(
        r"(?:class-[0-9-]+|item-[0-9]+|(?:status|narrow|equip|ce|hero)-[0-9]+|"
        r"banner-(?:jp-)?image-[0-9]+|banner-permanent-fallback)\.png"
    )
    image_root = source / "assets"
    if image_root.is_dir():
        for path in sorted(image_root.iterdir()):
            if image.fullmatch(path.name):
                marker = path.with_name(path.name + ".sha256")
                if not marker.is_file() or marker.stat().st_size > 256:
                    raise RuntimeError("图片缓存缺少校验记录")
                stamp = marker.read_text()
                if not re.fullmatch(r"[0-9a-f]{64}:[0-9a-f]{64}", stamp):
                    raise RuntimeError("图片缓存校验记录不合法")
                add("assets/" + path.name, stamp.split(":")[1])
                add("assets/" + marker.name)
    native = source / "native-ui"
    if native.is_dir():
        contracts = core / "infrastructure/native_support_ui"
        hashes = json.loads((contracts / "asset_hashes.json").read_bytes())
        for name, digest in hashes.items():
            add("native-ui/" + name, digest)
        font = json.loads((contracts / "font_manifest.json").read_bytes())
        add("native-ui/" + font["key"], font["sha256"], font["bytes"])
    fonts = {
        "NotoSansCJK-Regular.ttc": (
            "d18a3adba5c891eb7f9cdf8c31dad28dcc324db80e662d6a0f91e8db6aee5913"
        ),
        "DejaVuSans.ttf": "f9a7bd58cdaa9d1e9acafd070db8dea0b6db9cf2b540501b15075315a47cba3d",
    }
    for name, digest in fonts.items():
        if (source / "fonts" / name).is_file():
            add("fonts/" + name, digest)
    destination.mkdir(parents=True)
    for relative in expected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    marker = {
        name.removeprefix("assets/"): digest
        for name, digest in expected.items()
        if name.startswith("assets/")
    }
    (destination / "SEED_IMAGES_MANIFEST.json").write_text(json.dumps(marker, indent=2))
    manifest = destination / "SEED_IMAGES_MANIFEST.json"
    expected[manifest.name] = sha(manifest.read_bytes())
    actual = {
        path.relative_to(destination).as_posix(): sha(path.read_bytes())
        for path in destination.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise RuntimeError("资源暂存目录与白名单不符")
    return expected

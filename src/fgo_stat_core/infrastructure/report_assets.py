"""只按公开主数据规划报告所需图像，不传输统计、好友码或抓包。"""

import asyncio
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fgo_stat_core.contracts.models import CaptureError
from fgo_stat_core.contracts.report import AnnualReport
from fgo_stat_core.infrastructure.public_download import ImageDownloader, PublicImageDownloader
from fgo_stat_core.infrastructure.report_master import _part_names, _read

__all__ = ["AssetSpec", "fetch_report_assets", "plan_report_assets"]
_BASE = "https://static.atlasacademy.io"
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class AssetSpec:
    filename: str
    url: str
    transparent: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            not re.fullmatch(r"[a-z0-9-]+\.png", self.filename)
            or parsed.scheme != "https"
            or parsed.netloc != "static.atlasacademy.io"
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"/(?:JP|CN)/[A-Za-z]+/[A-Za-z0-9_/@.-]+\.png", parsed.path)
            or ".." in parsed.path
        ):
            raise CaptureError("REPORT_ASSET_INVALID")


def _first_asset(row: dict[str, Any], kind: str, group: str) -> str | None:
    candidates = row.get("extraAssets", {}).get(kind, {}).get(group, {})
    if not isinstance(candidates, dict):
        return None
    keys = sorted((k for k in candidates if str(k).isdigit()), key=lambda k: int(k))
    key = "1" if "1" in candidates else next(iter(keys), None)
    result = candidates.get(key) if key is not None else None
    return result if isinstance(result, str) else None


def plan_report_assets(
    report: AnnualReport, data_dir: Path, *, native_support: bool = False
) -> tuple[AssetSpec, ...]:
    """只读固定公共字典。文件清单不包含任何账号统计或好友码。"""
    try:
        manifest = json.loads((data_dir / "SOURCE_MANIFEST.json").read_bytes())
        servants = {
            row["id"]: row
            for filename in _part_names(manifest, "servants")
            for row in _read(data_dir, filename, manifest)
        }
        ces = {
            row["id"]: row
            for filename in _part_names(manifest, "craftEssences")
            for row in _read(data_dir, filename, manifest)
        }
        items = {row["id"]: row for row in _read(data_dir, "items.json", manifest)}
        skills = (
            {v["id"]: v for v in _read(data_dir, "baseSkills.json", manifest)}
            if any(v["filename"] == "baseSkills.json" for v in manifest["files"])
            else {}
        )
        return _plan(report, servants, ces, items, skills, native_support=native_support)
    except CaptureError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise CaptureError("MASTER_DATA_INVALID") from None


def _plan(
    report: AnnualReport,
    servants: dict[int, dict[str, Any]],
    ces: dict[int, dict[str, Any]],
    items: dict[int, dict[str, Any]],
    skills: dict[int, dict[str, Any]] | None = None,
    *,
    native_support: bool = False,
) -> tuple[AssetSpec, ...]:
    jobs: dict[str, AssetSpec] = {}

    def add(filename: str, url: str | None, transparent: bool = False) -> None:
        if url:
            jobs[filename] = AssetSpec(filename, url, transparent)

    # ConstData.classInfo.iconImageId 与 SvtClassX.icon 的已核对映射。
    for cid in (1, 2, 3, 4, 5, 6, 7, 1001, 1002, 1004, 1005):
        add(f"class-{cid}.png", f"{_BASE}/JP/ClassIcons/class3_{cid}.png")
    for iid in (2, 1000, 1001, 6999, 7999, 17):
        add(f"item-{iid}.png", items.get(iid, {}).get("icon"))
    favorites = (report.favorites.value or ())[:2]
    servant_ids = {v.servant_id for v in favorites}
    ce_ids: set[int] = set()
    if report.supports and report.supports.value:
        for group in report.supports.value[:20]:
            for slot in group.slots:
                if slot.state == "ready" and slot.servant_id is not None:
                    servant_ids.add(slot.servant_id)
                    if slot.craft_essence:
                        ce_ids.add(slot.craft_essence.craft_essence_id)
    if report.grand_supports and report.grand_supports.value:
        for grand_entry in report.grand_supports.value[:20]:
            if grand_entry.servant.state == "ready" and grand_entry.servant.servant_id is not None:
                servant_ids.add(grand_entry.servant.servant_id)
            for equipment in grand_entry.craft_essences:
                if equipment.craft_essence:
                    ce_ids.add(equipment.craft_essence.craft_essence_id)
    support_slots = (
        [slot for group in (report.supports.value or ()) for slot in group.slots]
        if report.supports
        else []
    )
    if report.grand_supports:
        support_slots.extend(entry.servant for entry in (report.grand_supports.value or ()))
    for slot in support_slots:
        # 原生编组用单个 main/append mini 切换，不下载未展示的逐技能图标。
        if native_support:
            continue
        for key in (slot.class_icon_key, slot.grand_class_icon_key):
            if key:
                match = re.fullmatch(r"class-([0-3])-([1-9][0-9]*)\.png", key)
                if match:
                    add(key, f"{_BASE}/JP/ClassIcons/class{match[1]}_{match[2]}.png")
        if slot.active_skill_icons:
            for icon in slot.active_skill_icons.value or ():
                if not icon.icon_key:
                    continue
                if icon.source == "captured_id":
                    match = re.fullmatch(r"skill-([1-9][0-9]*)\.png", icon.icon_key)
                    if match:
                        add(icon.icon_key, (skills or {}).get(int(match[1]), {}).get("icon"))
                elif slot.servant_id is not None and icon.source == "shared_static_icon":
                    entries = servants.get(slot.servant_id, {}).get("skills", [])
                    candidates = [
                        (skills or {}).get(v["id"], {}).get("icon")
                        for v in entries
                        if v.get("num") == icon.position
                    ]
                    if candidates and len(set(candidates)) == 1 and candidates[0]:
                        add(icon.icon_key, candidates[0])
        for metric in (slot.append_skills,):
            for skill in metric.value or () if metric else ():
                if skill and skill.skill_id is not None and skill.icon_key:
                    add(skill.icon_key, (skills or {}).get(skill.skill_id, {}).get("icon"))
    for sid in sorted(servant_ids):
        add(f"status-{sid}.png", _first_asset(servants.get(sid, {}), "status", "ascension"), True)
        if native_support:
            add(
                f"narrow-{sid}.png",
                _first_asset(servants.get(sid, {}), "narrowFigure", "ascension"),
            )
    if favorites:
        selected = next((v for v in favorites if "push" in v.roles), favorites[0])
        sid = selected.servant_id
        add(f"hero-{sid}.png", _first_asset(servants.get(sid, {}), "charaGraph", "ascension"))
    for cid in sorted(ce_ids):
        add(f"ce-{cid}.png", _first_asset(ces.get(cid, {}), "faces", "equip"))
        if native_support:
            # Chaldea Servant.equipFace 使用 equip[id]，不能截取普通 faces 冒充。
            equip = ces.get(cid, {}).get("extraAssets", {}).get("equipFace", {}).get("equip", {})
            add(f"equip-{cid}.png", equip.get(str(cid)) if isinstance(equip, dict) else None)
    if report.gachas.value:
        region = report.metadata.region
        prefix = "jp-" if region == "JP" else ""
        for entry in report.gachas.value.all_time[:3] + report.gachas.value.selected_pool_year[:3]:
            banner_id = entry.banner_image_id
            if banner_id:
                add(
                    f"banner-{prefix}image-{banner_id}.png",
                    f"{_BASE}/{region}/SummonBanners/img_summon_{banner_id}.png",
                )
    # 用户明确批准缺池图回退：CN gacha500139 剧情常驻池 imageId223。
    if report.metadata.region == "CN":
        add("banner-permanent-fallback.png", f"{_BASE}/CN/SummonBanners/img_summon_223.png")
    return tuple(jobs.values())


def _validate_png(data: bytes, transparent: bool) -> None:
    if len(data) > _MAX_IMAGE_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CaptureError("REPORT_IMAGE_INVALID")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as picture:
            if picture.width * picture.height > 8_000_000 or max(picture.size) > 4096:
                raise CaptureError("REPORT_IMAGE_TOO_LARGE")
            picture.load()
            if transparent and (
                "A" not in picture.getbands() or picture.getchannel("A").getextrema()[0] != 0
            ):
                raise CaptureError("REPORT_IMAGE_NOT_TRANSPARENT")
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("REPORT_IMAGE_INVALID") from None


async def fetch_report_assets(
    specs: tuple[AssetSpec, ...],
    cache_dir: Path,
    downloader: ImageDownloader | None = None,
    *,
    repair_cache: bool = False,
) -> dict[str, int]:
    """有界下载公开素材到独立缓存；失败缺图不冒充其他角色。返回计数。"""
    if len(specs) > 256 or len({v.filename for v in specs}) != len(specs):
        raise CaptureError("REPORT_ASSET_LIMIT")
    if cache_dir.is_symlink():
        raise CaptureError("REPORT_ASSET_PATH_INVALID")
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fetcher = downloader or PublicImageDownloader(limit=_MAX_IMAGE_BYTES, timeout=25)
    gate = asyncio.Semaphore(4)
    counts = {"available": 0, "missing": 0, "downloaded": 0}

    async def fetch(spec: AssetSpec) -> None:
        async with gate:
            path = cache_dir / spec.filename
            stamp = cache_dir / (spec.filename + ".sha256")
            if path.is_symlink() or stamp.is_symlink():
                raise CaptureError("REPORT_ASSET_PATH_INVALID")
            existing = path.exists()
            if existing or stamp.exists():
                try:
                    if path.stat().st_size > _MAX_IMAGE_BYTES or stamp.stat().st_size > 256:
                        raise CaptureError("REPORT_IMAGE_INVALID")
                    raw = path.read_bytes()
                    expected = (
                        hashlib.sha256(spec.url.encode()).hexdigest()
                        + ":"
                        + hashlib.sha256(raw).hexdigest()
                    )
                    if stamp.read_text() != expected:
                        raise CaptureError("REPORT_ASSET_CACHE_MISMATCH")
                    _validate_png(raw, spec.transparent)
                except (CaptureError, OSError, UnicodeError):
                    if not repair_cache:
                        raise CaptureError("REPORT_ASSET_CACHE_INVALID") from None
                    # 仅清理本次固定素材名，地址变化或中断下载都可重新修复。
                    path.unlink(missing_ok=True)
                    stamp.unlink(missing_ok=True)
                    existing = False
            try:
                if existing:
                    counts["available"] += 1
                    return
                else:
                    raw = await fetcher.fetch(spec.url)
                    _validate_png(raw, spec.transparent)
                    marker = (
                        hashlib.sha256(spec.url.encode()).hexdigest()
                        + ":"
                        + hashlib.sha256(raw).hexdigest()
                    )
                    temporary = []
                    try:
                        for target, payload in ((path, raw), (stamp, marker.encode())):
                            with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as handle:
                                partial = Path(handle.name)
                                temporary.append((partial, target))
                                handle.write(payload)
                        for partial, target in temporary:
                            os.replace(partial, target)
                    finally:
                        for partial, _ in temporary:
                            partial.unlink(missing_ok=True)
                    counts["downloaded"] += 1
                counts["available"] += 1
            except (CaptureError, OSError):
                if existing:
                    # 不让已存在但未通过核验的图片进入 renderer 的文件存在判断。
                    raise CaptureError("REPORT_ASSET_CACHE_INVALID") from None
                counts["missing"] += 1

    await asyncio.gather(*(fetch(spec) for spec in specs))
    return counts

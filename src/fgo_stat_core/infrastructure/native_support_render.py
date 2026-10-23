"""有界原生助战渲染桥：仅内存图片与固定资源键，不落盘报告中间件。"""

import hashlib
import html
import io
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, cast

from PIL import Image

from fgo_stat_core.contracts.models import CaptureError
from fgo_stat_core.contracts.report import AnnualReport
from fgo_stat_core.infrastructure.native_support_adapter import native_panels
from fgo_stat_core.infrastructure.report_assets import _validate_png

_ROOT = Path(__file__).parent / "native_support_ui"


class NativeAssetBank:
    """固定发行清单校验原生纹理；公开角色图限定最小文件名与根目录。"""

    def __init__(self, asset_dir: Path, native_ui_dir: Path, hashes: dict[str, str]) -> None:
        if asset_dir.is_symlink() or native_ui_dir.is_symlink():
            raise CaptureError("REPORT_RESOURCE_OUT_OF_ROOT")
        self.assets = asset_dir.resolve()
        self.native = native_ui_dir.resolve()
        self.hashes = hashes
        self.images: dict[str, Image.Image] = {}
        self.total_bytes = 0

    def formation_font(self) -> bytes:
        """固定原生字体清单；不接受由编队名或报告输入决定的资源路径。"""
        spec = json.loads((_ROOT / "font_manifest.json").read_bytes())
        key = "font/FGO-Main-Font.otf"
        if (
            spec.get("key") != key
            or type(spec.get("bytes")) is not int
            or not 1 <= spec["bytes"] <= 16 * 1024 * 1024
            or not re.fullmatch(r"[0-9a-f]{64}", spec.get("sha256", ""))
        ):
            raise CaptureError("REPORT_NATIVE_FONT_INVALID")
        path = self.native / key
        if (
            path.is_symlink()
            or path.parent.is_symlink()
            or self.native not in path.resolve().parents
        ):
            raise CaptureError("REPORT_RESOURCE_OUT_OF_ROOT")
        try:
            if path.stat().st_size != spec["bytes"]:
                raise CaptureError("REPORT_NATIVE_FONT_INVALID")
            raw = path.read_bytes()
        except OSError:
            raise CaptureError("REPORT_NATIVE_FONT_MISSING") from None
        if len(raw) != spec["bytes"] or hashlib.sha256(raw).hexdigest() != spec["sha256"]:
            raise CaptureError("REPORT_NATIVE_FONT_INVALID")
        return raw

    def __call__(self, key: str) -> Image.Image:
        if key in self.images:
            return self.images[key].copy()
        if key in self.hashes:
            root, expected = self.native, self.hashes[key]
        elif re.fullmatch(r"(?:narrow|equip)-[1-9][0-9]*\.png", key):
            root, expected = self.assets, None
        else:
            raise CaptureError("REPORT_NATIVE_ASSET_UNKNOWN")
        path = root / key
        if path.is_symlink() or root not in path.resolve().parents:
            raise CaptureError("REPORT_RESOURCE_OUT_OF_ROOT")
        # 禁止经中间目录符号链接离开受控资源根，即使最终 resolve 恰好折返。
        if any(
            parent.is_symlink()
            for parent in path.parents
            if parent != root and root in parent.parents
        ):
            raise CaptureError("REPORT_RESOURCE_OUT_OF_ROOT")
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                raise CaptureError("REPORT_IMAGE_INVALID")
            raw = path.read_bytes()
        except OSError:
            raise CaptureError("REPORT_IMAGE_MISSING") from None
        if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
            raise CaptureError("REPORT_NATIVE_ASSET_HASH_MISMATCH")
        _validate_png(raw, False)
        self.total_bytes += len(raw)
        if len(self.images) >= 256 or self.total_bytes > 64 * 1024 * 1024:
            raise CaptureError("REPORT_ASSET_LIMIT")
        with Image.open(io.BytesIO(raw)) as image:
            self.images[key] = image.convert("RGBA")
        return self.images[key].copy()


def render_native_blocks(
    report: AnnualReport,
    asset_dir: Path,
    native_ui_dir: Path,
    *,
    demo: bool = False,
    header_font: Path | None = None,
    regular_font: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[str, dict[str, bytes]]:
    from fgo_stat_core.infrastructure.native_support_ui.renderer import NativeSlot, build_image

    hashes: dict[str, str] = json.loads((_ROOT / "asset_hashes.json").read_bytes())
    bank = NativeAssetBank(asset_dir, native_ui_dir, hashes)
    blocks: list[str] = []
    images: dict[str, bytes] = {}
    panels = native_panels(report)
    if progress is not None:
        progress(0, len(panels))
    if report.supports is not None:
        if report.supports.value is None:
            blocks.append('<p class="support-note">普通／活动助战数据未提供或不完整</p>')
        elif not report.supports.value:
            blocks.append('<p class="support-note">无普通／活动助战编组</p>')
        elif report.supports.state == "partial":
            blocks.append(
                '<p class="support-note">普通／活动助战信息不完整，以下仅展示已知数据</p>'
            )
    needs_font = any(
        panel.formation_name
        or any(
            ce.get("effect_mode") and ce.get("effect_mode_evidence") == "inferred"
            for slot in panel.slots
            for ce in slot.get("equipment", ())
        )
        for panel in panels
    )
    font_bytes = bank.formation_font() if needs_font else None
    for number, panel in enumerate(panels):
        if cancelled is not None and cancelled():
            raise CaptureError("CANCELLED")
        title = "冠位助战" if panel.card == "grand" else panel.title.split(" · ", 1)[0]
        blocks.append(f'<h3 style="margin:20px 0 8px">{html.escape(title)}</h3>')
        states: tuple[Literal["both"], ...] = ("both",)
        for state in states:
            view = build_image(
                panel.card,
                state,
                cast(Sequence[NativeSlot], panel.slots),
                bank,
                demo=demo,
                title=title,
                formation_name=panel.formation_name,
                formation_name_font=font_bytes,
                **({"header_font": str(header_font)} if header_font is not None else {}),
                **({"regular_font": str(regular_font)} if regular_font is not None else {}),
            )
            expected_size = (
                (1138, 484) if panel.card == "ordinary"
                else (1410, 569) if len(panel.slots) == 11 else (1164, 569)
            )
            if view.size != expected_size or view.mode != "RGBA":
                raise CaptureError("REPORT_NATIVE_CANVAS_INVALID")
            alpha = view.getchannel("A")
            if any(
                alpha.getpixel(point) != 0
                for point in (
                    (0, 0),
                    (view.width - 1, 0),
                    (0, view.height - 1),
                    (view.width - 1, view.height - 1),
                )
            ):
                raise CaptureError("REPORT_NATIVE_ALPHA_INVALID")
            data = io.BytesIO()
            view.save(data, format="PNG")
            raw = data.getvalue()
            _validate_png(raw, False)
            # 只有由本函数产生的固定键可在 renderer 中走内存图片分支。
            uri = f"report-native:{number}-{state}.png"
            images[uri] = raw
            blocks.append(
                f'<img src="{uri}" alt="{html.escape(title, quote=True)}" '
                'style="display:block;width:100%;height:auto;margin:0 0 10px">'
            )
        if progress is not None:
            progress(number + 1, len(panels))
    if not panels and report.grand_supports is not None:
        blocks.append('<p class="support-note">冠位助战数据未提供</p>')
    return "".join(blocks), images

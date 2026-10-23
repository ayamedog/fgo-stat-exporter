"""原生助战条绘制；国服九列，日服戴冠十一列，扩展列复用额外职阶样式。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

from PIL import Image, ImageChops, ImageDraw, ImageFont

from fgo_stat_core.contracts.formation_name import validate_formation_name
from fgo_stat_core.infrastructure.report_fonts import report_font_path

from .bitmap_font import NativeBitmapFont
from .typography import SCALE as TEXT_SCALE
from .typography import bitmap_line, ink_line

ImageSource = Callable[[str], Image.Image]


class NativeAppend(TypedDict):
    num: int
    state: Literal["unlocked", "locked", "unavailable"]
    level: int | None


class NativeEquipment(TypedDict):
    state: Literal["ready", "empty", "unavailable"]
    id: NotRequired[int | None]
    limit_break_count: NotRequired[int | None]
    effect_mode: NotRequired[Literal["original", "starting_np_50"] | None]
    effect_mode_confirmed: NotRequired[bool]
    effect_mode_evidence: NotRequired[Literal["inferred", "confirmed"] | None]


class NativeSlot(TypedDict):
    state: Literal["ready", "empty", "unreleased", "unavailable"]
    servant_id: NotRequired[int]
    class_icon: NotRequired[str | None]
    level: NotRequired[int | None]
    np_level: NotRequired[int | None]
    skill_levels: NotRequired[Sequence[int | None]]
    append_skills: NotRequired[Sequence[NativeAppend] | None]
    equipment: NotRequired[Sequence[NativeEquipment]]
    parameters: NotRequired[dict[str, int | None]]
    frame_key: NotRequired[str | None]
    frame_bg_key: NotRequired[str | None]


META = json.loads((Path(__file__).parent / "native_geometry.json").read_text())
G = META["templates"]
A = META["audit"]
FONT = report_font_path("cjk")


class AssetBank:
    def __init__(self, source: ImageSource) -> None:
        self.source = source

    def sprite(self, name: str | None) -> Image.Image:
        if name is None:
            raise ValueError("NATIVE_ASSET_NAME_REQUIRED")
        return self.source("native/" + name + ".png").convert("RGBA")


def node(card: str, suffix: str) -> Any:
    ns = [n for n in A["cards"][card]["nodes"] if n["path"].endswith(suffix)]
    assert len(ns) == 1, (card, suffix, len(ns))
    return ns[0]


def rect(card: str, suffix: str) -> Any:
    n = node(card, suffix)
    cs = [c for c in n["components"] if "card_root_bounds_xyxy" in c]
    assert len(cs) == 1
    return cs[0]["card_root_bounds_xyxy"]


def patch_size(bounds: Sequence[float], w: float, h: float, pivot: str) -> list[float]:
    left, b, r, t = bounds
    if pivot == "Left":
        return [left, (b + t - h) / 2, left + w, (b + t + h) / 2]
    if pivot == "Top":
        return [(left + r - w) / 2, t - h, (left + r + w) / 2, t]
    raise ValueError(pivot)


class Scene:
    bmf: NativeBitmapFont

    def __init__(
        self, card: str, width: int = 1200, font: str = FONT, regular_font: str | None = None,
        extra_columns: int = 0,
    ) -> None:
        self.regular_font = regular_font
        self.font = font
        self.card = card
        self.scale = (width - 48) / 1024
        self.width = width + round(extra_columns * 110 * self.scale)
        self.height = 670 if card == "ordinary" else 820
        self.origin = (self.width / 2, 360 if card == "ordinary" else 420)
        self.im = Image.new("RGBA", (self.width, self.height))
        self.layers: list[dict[str, Any]] = []

    def xy(self, x: float, y: float) -> tuple[float, float]:
        return (self.origin[0] + x * self.scale, self.origin[1] - y * self.scale)

    def paste(
        self,
        img: Image.Image,
        bounds: Sequence[float],
        cpos: Sequence[float],
        cscale: Sequence[float],
        role: str,
    ) -> None:
        left, b, r, t = bounds
        cx, cy = cpos
        sx, sy = cscale
        b2 = [cx + left * sx, cy + b * sy, cx + r * sx, cy + t * sy]
        x1, y1 = self.xy(b2[0], b2[3])
        x2, y2 = self.xy(b2[2], b2[1])
        box = [round(x1), round(y1), round(x2), round(y2)]
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        self.im.alpha_composite(
            img.resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS),
            (box[0], box[1]),
        )
        self.layers.append({"role": role, "native_bounds": b2, "pixel_bounds": box})

    def label(
        self, text: str, x: float, y: float, sz: int = 17, fill: str = "#415066", anchor: str = "mm"
    ) -> None:
        dr = ImageDraw.Draw(self.im)
        dr.text((x, y), text, font=ImageFont.truetype(self.font, sz), fill=fill, anchor=anchor)


def draw_number(
    scene: Scene,
    layer: dict[str, Any],
    cp: Sequence[float],
    cs: Sequence[float],
    text: str,
    kind: str,
) -> None:
    sx, sy = layer["scale"][:2]
    x, y = layer["position"][:2]
    if kind == "skill_levels" and "/SkillLevelTreasureDeviceBase/" in layer["path"]:
        x += 16 * sx
    if kind == "np_level":
        # 公开国服实机量测的文本组位置修正，非已解出的运行时 Set。
        x += 24 * sx
    limit = 122 if kind == "skill_levels" and "/AppendSkillLevelBase/" in layer["path"] else None
    im, w, h, boxes = bitmap_line(text, 16, scene.bmf, 10, limit, regular_font=scene.regular_font)
    scene.paste(
        im, [x - 2 * sx, y - (h - 2) * sy + 8 * sy, x + (w - 2) * sx, y + 10 * sy], cp, cs, kind
    )


def native_nine_slice(image: Image.Image, width: int, height: int, border: int = 24) -> Image.Image:
    out = Image.new("RGBA", (width, height))
    xs = [0, border, image.width - border, image.width]
    ys = [0, border, image.height - border, image.height]
    xd = [0, border, width - border, width]
    yd = [0, border, height - border, height]
    for row in range(3):
        for col in range(3):
            cell = image.crop((xs[col], ys[row], xs[col + 1], ys[row + 1]))
            cell = cell.resize(
                (xd[col + 1] - xd[col], yd[row + 1] - yd[row]), Image.Resampling.LANCZOS
            )
            out.alpha_composite(cell, (xd[col], yd[row]))
    return out


def render_formation_name(name: str, font_data: bytes) -> Image.Image:
    """Native UILabel 2587: font20, centered white, 960x38; no system fallback.

    Names are preserved verbatim and must satisfy the report contract. Long input
    is proportionally scaled within the native label rectangle, without truncation.
    Missing glyphs cause a clear error, never silent fallback/tofu.
    """
    scale = 4
    width = 960 * scale
    height = 38 * scale
    text = name
    validate_formation_name(text)
    out = Image.new("RGBA", (width, height))
    if not text:
        return out
    font = ImageFont.truetype(BytesIO(font_data), 20 * scale)
    missing = bytes(font.getmask(chr(0x10FFFF)))
    for ch in set(text):
        if not ch.isspace() and bytes(font.getmask(ch)) == missing:
            raise ValueError("Native formation-name font is missing a required glyph")
    bbox = font.getbbox(text, anchor="mm")
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    # Render from the actual ink bounds so the user-visible text is centered.
    ink = Image.new("RGBA", (round(max(w, 1)) + 8 * scale, round(max(h, 1)) + 8 * scale))
    ImageDraw.Draw(ink).text(
        (4 * scale - bbox[0], 4 * scale - bbox[1]),
        text,
        font=font,
        anchor="mm",
        fill="white",
        stroke_width=scale,
        stroke_fill=(0, 0, 0, 255),
    )
    ink_box = ink.getchannel("A").getbbox()
    if ink_box is None:
        return out
    ink = ink.crop(ink_box)
    ratio = min(1, (width - 16 * scale) / ink.width, (height - 4 * scale) / ink.height)
    if ratio < 1:
        ink = ink.resize(
            (max(1, round(ink.width * ratio)), max(1, round(ink.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    out.alpha_composite(ink, ((width - ink.width) // 2, (height - ink.height) // 2))
    return out


def render_effect_label(mode: str, font_data: bytes, *, inferred: bool = False) -> Image.Image:
    """Explicit requested CE2 annotation, not a reconstructed native status label."""
    # User requested concise visible labels. Evidence remains inferred in the
    # report/adapter; presentation does not promote it to confirmed.
    labels = (
        {"original": "原效果", "starting_np_50": "NP50%"}
        if inferred
        else {"original": "原礼装效果", "starting_np_50": "初始NP 50%"}
    )
    if mode not in labels:
        raise ValueError("Unsupported grand CE2 effect mode")
    text = labels[mode]
    scale = 4
    font = ImageFont.truetype(BytesIO(font_data), 10 * scale)
    missing = bytes(font.getmask(chr(0x10FFFF)))
    if any(not ch.isspace() and bytes(font.getmask(ch)) == missing for ch in set(text)):
        raise ValueError("Native effect-label font is missing a required glyph")
    canvas = Image.new("RGBA", (96 * scale, 15 * scale))
    box = font.getbbox(text, anchor="mm")
    # White text uses a restrained dark stroke over the original CE art.
    ImageDraw.Draw(canvas).text(
        (canvas.width / 2 - (box[0] + box[2]) / 2, canvas.height / 2 - (box[1] + box[3]) / 2),
        text,
        anchor="mm",
        font=font,
        fill="white",
        stroke_width=2,
        stroke_fill=(0, 0, 0, 235),
    )
    return canvas


def build_image(
    card: Literal["ordinary", "grand"],
    state: Literal["main", "append", "both"],
    slots: Sequence[NativeSlot],
    assets: ImageSource,
    *,
    demo: bool = False,
    title: str | None = None,
    header_font: str = FONT,
    regular_font: str | None = None,
    formation_name: str | None = None,
    formation_name_font: bytes | None = None,
) -> Image.Image:
    """纯内存RGBA原生面板；demo/title保留兼容，但不绘制外层文字。"""
    if card not in ["ordinary", "grand"] or state not in ["main", "append", "both"]:
        raise ValueError("Unsupported native state")
    if len(slots) != 9 and not (card == "grand" and len(slots) == 11):
        raise ValueError("NATIVE_SLOT_COUNT_INVALID")
    extra_columns = len(slots) - 9
    frames = True
    labels = False
    ATLAS = AssetBank(assets)
    t = next(
        x for x in G if x["card"] == card and x["state"] == ("main" if state == "both" else state)
    )
    if extra_columns:
        t = deepcopy(t)
        # 新增两列复用原第八、九列，横向间隔仍为 110，不改动单栏内部坐标。
        for index in (7, 8):
            column = deepcopy(t["columns"][index])
            column["position"][0] += 220
            t["columns"].append(column)
        for column in t["columns"]:
            column["position"][0] -= 110
    scene = Scene(card, font=header_font, regular_font=regular_font, extra_columns=extra_columns)
    raw = cast(Sequence[dict[str, Any]], slots)
    scene.bmf = NativeBitmapFont(META["font"], assets("font/FGO-NumberFont-01.png"))
    if card == "ordinary":
        scene.paste(
            ATLAS.sprite("formation_support_bg2"),
            [-506, -190, 506, 240],
            [0, 0],
            [1, 1],
            "native_support_panel",
        )
    else:
        scene.paste(
            native_nine_slice(ATLAS.sprite("formation_grand_bg"), 1034 + extra_columns * 110, 506),
            [-517 - extra_columns * 55, -281, 517 + extra_columns * 55, 225],
            [0, 0],
            [1, 1],
            "native_grand_support_panel",
        )
    # 背景 alpha 不是内容透明度：冠位背景内部本来就是半透明的。
    # 只在 nine-slice 四角保留原生轮廓裁切；其余区域按正常 alpha 合成，
    # 否则最后取 darker 会把不透明从者、礼装和文字一起压成半透明。
    panel_alpha = scene.im.getchannel("A").copy()
    panel_box = cast(tuple[int, int, int, int], tuple(scene.layers[-1]["pixel_bounds"]))
    if card == "grand":
        left, top, right, bottom = panel_box
        corner = round(24 * scene.scale)  # 与 native_nine_slice 的固定边界一致。
        clip = ImageDraw.Draw(panel_alpha)
        clip.rectangle((left + corner, top, right - corner - 1, bottom - 1), fill=255)
        clip.rectangle((left, top + corner, right - 1, bottom - corner - 1), fill=255)
    for i, (col, slot) in enumerate(zip(t["columns"], raw, strict=True)):
        style_index = i if i < 9 else i - 2
        if slot["state"] not in ("ready", "empty", "unreleased", "unavailable"):
            raise ValueError("Invalid support state")
        if card == "ordinary" and slot["state"] == "unreleased":
            raise ValueError("Ordinary slots do not have an inferred unreleased state")
        cp = [
            col["position"][0] + t["container_offset"][0],
            col["position"][1] + t["container_offset"][1],
        ]
        cs = col["scale"]
        # Independent column headers are not the portrait's own class badges.
        onoff = "on" if slot["state"] == "ready" else "off"
        if card == "ordinary":
            code = ["1001", "001", "002", "003", "004", "005", "006", "007", "1002"][i]
            header = ATLAS.sprite(f"PartyOrganizationAtlas/icon_class_{onoff}{code}")
            scene.paste(
                header, [-33, -33, 33, 33], [-440.3 + 110 * i, 190.5], [1, 1], "column_header"
            )
        else:
            header = ATLAS.sprite(
                f"DownloadGrandServantListAtlas1/icon_class_{onoff}{style_index + 1:03d}"
            )
            scene.paste(header, [-34, -34, 34, 34], [cp[0], 180], [1, 1], "column_header")
        if slot["state"] != "ready":
            if card == "grand":
                name = (
                    f"formation_grand_locked_support_{style_index + 1:02d}"
                    if slot["state"] == "unreleased"
                    else f"formation_grand_blank_{style_index + 1:02d}"
                )
                empty = ATLAS.sprite("DownloadGrandServantListAtlas1/" + name)
                scene.paste(empty, [-59, -199, 59, 199], cp, cs, "empty_state_background")
            else:
                scene.paste(
                    ATLAS.sprite("PartyOrganizationAtlas/formation_blank_01"),
                    rect(card, "/BaseSprite"),
                    cp,
                    cs,
                    "empty_state_background",
                )
                scene.paste(
                    ATLAS.sprite("PartyOrganizationAtlas/icon_class" + code),
                    rect(card, "/BackClassIcon"),
                    cp,
                    cs,
                    "empty_class_background",
                )
                scene.paste(
                    ATLAS.sprite("formation_blank_02"),
                    rect(card, "/EquipObject/NoneEquipSprite"),
                    cp,
                    cs,
                    "empty_equipment_background",
                )
            continue
        if frames and slot.get("frame_bg_key"):
            if card == "ordinary":
                scene.paste(
                    ATLAS.sprite(slot["frame_bg_key"]),
                    rect(card, "/BaseSprite"),
                    cp,
                    cs,
                    "frame_back",
                )
            else:
                scene.paste(
                    ATLAS.sprite(slot["frame_bg_key"]),
                    rect(card, "/Background"),
                    cp,
                    cs,
                    "frame_back",
                )
        portrait = assets(f"narrow-{slot['servant_id']}.png").convert("RGBA")
        if card == "ordinary" and not frames:
            # Preserve portrait transform; hide only the area formerly covered by the frame.
            # Native CE lower edge -130, body top186.5/height373. No card/CE movement.
            cutoff = round((186.5 + 130) / 373 * portrait.height)
            alpha = portrait.getchannel("A")
            ImageDraw.Draw(alpha).rectangle((0, cutoff, portrait.width, portrait.height), fill=0)
            portrait.putalpha(alpha)
        scene.paste(portrait, t["shared_layers"][0]["bounds"], cp, cs, "servant_art")
        if frames and slot.get("frame_key"):
            if card == "ordinary":
                scene.paste(
                    ATLAS.sprite(slot["frame_key"]), rect(card, "/Base2Sprite"), cp, cs, "frame"
                )
            else:
                scene.paste(ATLAS.sprite(slot["frame_key"]), rect(card, "/Frame"), cp, cs, "frame")
        if not slot.get("frame_key") or not slot.get("frame_bg_key"):
            x, y = scene.xy(cp[0], cp[1] - 210 * cs[1])
            scene.label("框型未提供", x, y, 12, fill="white")
        # Real servant class; EX column index is never used as servant class.
        class_name = slot.get("class_icon")
        if not class_name:
            raise ValueError(
                "Ready slot needs actual servant native class icon; do not infer ALL/EXTRA"
            )
        match = re.fullmatch(r"class([0-3])_(\d+)", class_name)
        if not match:
            raise ValueError("Native class icon must be class{color}_{iconId}")
        color, class_id = match.groups()
        scene.paste(
            ATLAS.sprite(f"class{color}_{class_id}"),
            t["shared_layers"][1]["bounds"],
            cp,
            cs,
            "class_icon",
        )
        cname = (
            "/ClassIcon/ClassNameSprite"
            if card == "ordinary"
            else "/ClassIcon/Scale/ClassNameSprite"
        )
        scene.paste(
            ATLAS.sprite(f"class{color}_txt_{class_id}"), rect(card, cname), cp, cs, "class_name"
        )
        expected_eq = 1 if card == "ordinary" else 3
        eqs = slot.get("equipment", [])
        if len(eqs) != expected_eq:
            raise ValueError("Equipment slots must preserve all required positions")
        for n, eq in enumerate(eqs):
            bounds = t["shared_layers"][n + 2]["bounds"]
            if eq["state"] == "ready":
                if not eq.get("id"):
                    raise ValueError("Ready CE needs public CE id")
                scene.paste(
                    assets(f"equip-{eq['id']}.png").convert("RGBA"),
                    bounds,
                    cp,
                    cs,
                    f"equipment_{n + 1}",
                )
            elif eq["state"] == "empty":
                blank = (
                    "formation_blank_02"
                    if card == "ordinary"
                    else f"formation_blank_grand_0{n + 2}"
                )
                scene.paste(ATLAS.sprite(blank), bounds, cp, cs, f"equipment_{n + 1}_empty")
            else:
                blank = (
                    "formation_blank_02"
                    if card == "ordinary"
                    else f"formation_blank_grand_0{n + 2}"
                )
                scene.paste(ATLAS.sprite(blank), bounds, cp, cs, f"equipment_{n + 1}_unavailable")
                left, b, r, top = bounds
                x, y = scene.xy(cp[0] + (left + r) * cs[0] / 2, cp[1] + (b + top) * cs[1] / 2)
                scene.label("礼装未提供", x, y, 12, fill="white")
            if eq["state"] == "ready" and eq.get("limit_break_count") == 4:
                suf = (
                    "/EquipObject/EquipSprite/PointSprite"
                    if card == "ordinary"
                    else f"/EquipRoot/{n + 1}/PointSprite"
                )
                scene.paste(
                    ATLAS.sprite("icon_limit_on"), rect(card, suf), cp, cs, f"equipment_{n + 1}_mlb"
                )
        for n, eq in enumerate(eqs):
            mode = eq.get("effect_mode")
            evidence = eq.get("effect_mode_evidence")
            if evidence not in (None, "inferred", "confirmed"):
                raise ValueError("Unsupported CE2 evidence state")
            if eq.get("effect_mode_confirmed") is True:
                if evidence == "inferred":
                    raise ValueError("Conflicting CE2 evidence states")
                evidence = "confirmed"  # 仅保留兼容，当前生产投影只用inferred。
            if mode is None or evidence is None:
                continue
            if card != "grand" or n != 1 or eq["state"] != "ready":
                raise ValueError("Effect mode belongs only to ready grand CE2")
            if formation_name_font is None:
                raise ValueError("Native effect-label font is required")
            tag = render_effect_label(mode, formation_name_font, inferred=evidence == "inferred")
            scene.paste(tag, [-48, -103, 48, -88], cp, cs, "equipment_2_effect")
        # Original frame has permanent stat labels; missing values are never invented.
        for suffix, key, size in [
            ("/LevelText", "level", 18 if card == "ordinary" else 12),
            ("/HpText", "hp", 14 if card == "ordinary" else 10),
            ("/AttackText", "atk", 14 if card == "ordinary" else 10),
        ]:
            n = node(card, suffix)
            x, y = n["card_root_position_xyz"][:2]
            val = slot.get("level") if key == "level" else slot.get("parameters", {}).get(key)
            text = "?" if val is None else str(val) if key == "level" else f"{val:,}"
            im = ink_line(text, size, scene.bmf, key, regular_font=regular_font)
            w = im.width / TEXT_SCALE
            h = im.height / TEXT_SCALE
            right = x + size + 1 if key == "level" else x + size / 2
            if key == "level":
                y += 1.0 if card == "ordinary" else -0.5
            # 原生等级数字+量测基线；HP/ATK 的原字体缺失，使用已披露细体近似。
            maxwidth = (
                42
                if card == "ordinary" and key == "level"
                else 30
                if key == "level"
                else 90
                if card == "ordinary"
                else 65
            )
            if w > maxwidth:
                ratio = maxwidth / w
                im = im.resize(
                    (round(im.width * ratio), round(im.height * ratio)), Image.Resampling.LANCZOS
                )
                w = maxwidth
                h *= ratio
            scene.paste(im, [right - w, y - h / 2, right, y + h / 2], cp, cs, key + "_text")
        skill_states = ("main", "append") if state == "both" else (state,)
        for line_state in skill_states:
            source = next(x for x in G if x["card"] == card and x["state"] == line_state)
            for original_layer in source["skill_group"]["children"]:
                layer = deepcopy(original_layer)
                if state == "both" and line_state == "main":
                    dy = 24 * layer["scale"][1]
                    layer["position"][1] += dy
                    layer["bounds"][1] += dy
                    layer["bounds"][3] += dy
                if layer["role"] == "skill_group_icon":
                    sx, sy = layer["scale"][:2]
                    b = patch_size(layer["bounds"], 21 * sx, 21 * sy, "Left")
                    scene.paste(ATLAS.sprite(layer["asset_binding"]), b, cp, cs, "skill_group_icon")
                elif layer["role"] == "np_icon":
                    delta = 24 * layer["scale"][0]
                    bb = [
                        layer["bounds"][0] + delta,
                        layer["bounds"][1],
                        layer["bounds"][2] + delta,
                        layer["bounds"][3],
                    ]
                    scene.paste(ATLAS.sprite("icon_nplv"), bb, cp, cs, "np_icon")
                elif layer["role"] == "np_level":
                    draw_number(
                        scene,
                        layer,
                        cp,
                        cs,
                        str(slot.get("np_level")) if slot.get("np_level") is not None else "?",
                        "np_level",
                    )
                elif layer["role"] == "skill_levels":
                    if line_state == "main":
                        levels = slot.get("skill_levels", [None, None, None])
                        if len(levels) != 3:
                            raise ValueError("Three active skill levels required")
                        texts = [str(v) if v is not None else "?" for v in levels]
                    else:
                        app = slot.get("append_skills")
                        by_num = {a["num"]: a for a in app} if app is not None else {}
                        texts = []
                        for num in range(100, 105):
                            a = by_num.get(num)
                            if a is None or a["state"] == "unavailable":
                                texts.append("?")
                            elif a["state"] == "locked":
                                texts.append("-")
                            elif (
                                a["state"] == "unlocked"
                                and isinstance(a.get("level"), int)
                                and (1 <= a["level"] <= 10)
                            ):
                                texts.append(str(a["level"]))
                            else:
                                raise ValueError("Invalid unlocked append skill level")
                    draw_number(scene, layer, cp, cs, "/".join(texts), "skill_levels")
        if labels:
            for layer in t["skill_group"]["children"]:
                if layer["role"] in ["skill_levels", "np_level"]:
                    left, b, r, tt = layer["bounds"]
                    x, y = scene.xy(cp[0] + left * cs[0], cp[1] + tt * cs[1])
                    dr = ImageDraw.Draw(scene.im)
                    dr.rectangle(
                        (
                            x,
                            y,
                            x + max(8, (r - left) * cs[0] * scene.scale),
                            y + (tt - b) * cs[1] * scene.scale,
                        ),
                        outline=(255, 0, 210, 210),
                        width=1,
                    )
                    # A declared label box is evidence only, never a clip.
        # Names are not moved into card. Metadata stays outside strip in QA.
    if formation_name is not None:
        if card != "ordinary":
            raise ValueError("No native named-grand-deck label is established")
        validate_formation_name(formation_name)
        if formation_name:
            if formation_name_font is None:
                raise ValueError("Native formation-name font is required")
            name_image = render_formation_name(formation_name, formation_name_font)
            scene.paste(name_image, [-480, -169, 480, -131], [0, 0], [1, 1], "formation_name")
    # 保留原生抗锯齿边缘，不合成圆角或白底。
    scene.im.putalpha(ImageChops.darker(scene.im.getchannel("A"), panel_alpha))
    return scene.im.crop(panel_box)

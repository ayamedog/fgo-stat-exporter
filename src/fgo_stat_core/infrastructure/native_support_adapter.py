"""报告契约到原生九列的安全投影；不接触原始抓包或内部实例标识。"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from fgo_stat_core.contracts.models import CaptureError
from fgo_stat_core.contracts.report import AnnualReport, SupportCraftEssence, SupportSlot

_POSITIONS = (
    "all",
    "saber",
    "archer",
    "lancer",
    "rider",
    "caster",
    "assassin",
    "berserker",
    "extra",
)
_GRAND_GRAPHS = tuple(range(10001, 10010))
# 国服 2.129.0 的原生边框按 frameType 索引，仅接受已核验的 0..7。
# 不按稀有度、职阶配色或冠位状态猜测框型。
_GRAND_FRAME_SUFFIX = {
    0: 10002,
    1: 10000,
    2: 10000,
    3: 10000,
    4: 10001,
    5: 10001,
    6: 10003,
    7: 10003,
}


@dataclass(frozen=True)
class NativePanel:
    card: Literal["ordinary", "grand"]
    title: str
    slots: tuple[dict[str, Any], ...]
    formation_name: str | None = field(default=None, repr=False)


def _equipment(
    state: Literal["ready", "empty", "unavailable"] | None, ce: SupportCraftEssence | None
) -> dict[str, Any]:
    if ce is not None:
        return {
            "state": "ready",
            "id": ce.craft_essence_id,
            "limit_break_count": ce.limit_break_count,
        }
    return {"state": "empty" if state == "empty" else "unavailable"}


def _slot(slot: SupportSlot) -> dict[str, Any]:
    if slot.state != "ready":
        return {"state": slot.state}
    key = slot.grand_class_icon_key if slot.grand else slot.class_icon_key
    match = re.fullmatch(r"class-([0-3])-([1-9][0-9]*)\.png", key or "")
    # 保留已知从者、技能与数值；图标缺映射由 renderer 显式标缺，不能猜 ALL/EX。
    color = {0: 0, 1: 1, 2: 1, 3: 2, 4: 3, 5: 3}.get(slot.rarity if slot.rarity is not None else -1)
    frame_key = f"formation_frame_support{color}" if color is not None else None
    frame_bg_key = f"formation_framebg_{color}" if color is not None else None
    if slot.grand is True:
        # 冠位状态按 userSvtId 实例解析，不按从者种类合并。
        # 此处使用普通单礼装图片；三礼装冠位边框由单独组件处理。
        frame = _GRAND_FRAME_SUFFIX.get(
            slot.frame_type_base if slot.frame_type_base is not None else -1
        )
        frame_key = f"formation_frame_support{frame}" if frame is not None else None
        frame_bg_key = f"formation_framebg_{frame}" if frame is not None else None
    parameters = slot.parameters.value if slot.parameters else None
    return {
        "state": "ready",
        "servant_id": slot.servant_id,
        "class_icon": f"class{match[1]}_{match[2]}" if match else None,
        "level": slot.level,
        "np_level": slot.np_level,
        "skill_levels": slot.skill_levels,
        "append_skills": [
            {"num": item.num, "state": item.state, "level": item.level}
            for item in slot.append_skills.value
        ]
        if slot.append_skills and slot.append_skills.value is not None
        else None,
        "equipment": [_equipment(slot.craft_essence_state, slot.craft_essence)],
        "parameters": {
            "hp": parameters.hp if parameters else None,
            "atk": parameters.atk if parameters else None,
        },
        "frame_key": frame_key,
        "frame_bg_key": frame_bg_key,
    }


def native_panels(report: AnnualReport) -> tuple[NativePanel, ...]:
    """固定槽次序，保留所有已知编组；缺冠位行是未知，不是未解放。"""
    groups = report.supports.value or () if report.supports else ()
    grand_entries = report.grand_supports.value or () if report.grand_supports else ()
    if len(groups) > 20 or len(grand_entries) > 20:
        raise CaptureError("REPORT_RENDER_LIMIT")
    panels: list[NativePanel] = []
    for group in groups:
        by_position: dict[str, SupportSlot] = {slot.position: slot for slot in group.slots}
        panels.append(
            NativePanel(
                "ordinary",
                f"{'普通' if group.kind == 'normal' else '活动'}助战 · 编组 {group.index}",
                tuple(_slot(by_position[position]) for position in _POSITIONS),
                group.name,
            )
        )
    if report.grand_supports is not None:
        graphs = tuple(range(10001, 10012)) if report.metadata.region == "JP" else _GRAND_GRAPHS
        by_graph = {entry.graph_id: entry for entry in grand_entries}
        if len(by_graph) != len(grand_entries) or set(by_graph) - set(graphs):
            raise CaptureError("REPORT_GRAND_LAYOUT_UNMAPPED")
        slots: list[dict[str, Any]] = []
        for graph_id in graphs:
            entry = by_graph.get(graph_id)
            if entry is None:
                slots.append({"state": "unavailable"})
                continue
            item = _slot(entry.servant)
            # 客户端字段读取已核，但效果语义仍跨接Chaldea；将推定状态随标签传递。
            item["equipment"] = [
                {
                    **_equipment(ce.state, ce.craft_essence),
                    **(
                        {"effect_mode": ce.effect_mode, "effect_mode_evidence": "inferred"}
                        if ce.effect_mode is not None
                        else {}
                    ),
                }
                for ce in entry.craft_essences
            ]
            # 原生 GrandServantAssetSetting 明确映射；Great 两对同纹理，无需猜升格。
            frame = _GRAND_FRAME_SUFFIX.get(
                entry.servant.frame_type_base if entry.servant.frame_type_base is not None else -1
            )
            item["frame_key"] = f"formation_grand_frame_{frame}" if frame else None
            item["frame_bg_key"] = f"formation_grand_framebg_{frame}" if frame else None
            slots.append(item)
        panels.append(NativePanel("grand", "冠位编成 · 专用三礼装配置", tuple(slots)))
    return tuple(panels)

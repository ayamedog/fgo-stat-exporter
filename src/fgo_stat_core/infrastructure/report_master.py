"""只读加载本机已核验版本的公共字典。不会联网，也不处理私有抓包。"""

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from fgo_stat_core.contracts.models import CaptureError
from fgo_stat_core.contracts.report_master import (
    ClassBoardMaster,
    ClassGroup,
    CraftEssenceMaster,
    GachaMaster,
    QuestGroup,
    QuestMaster,
    ReportMasterData,
    ServantMaster,
    SkillMaster,
)

__all__ = ["load_report_master"]


def _read(root: Path, name: str, manifest: dict[str, Any]) -> Any:
    # 文件名来自代码固定清单，清单中的路径/URL 不用于读文件或发请求。
    expected = next((v for v in manifest["files"] if v["filename"] == name), None)
    if expected is None:
        raise CaptureError("MASTER_MANIFEST_INCOMPLETE")
    path = root / name
    if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
        raise CaptureError("MASTER_FILE_INVALID")
    raw = path.read_bytes()
    if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
        raise CaptureError("MASTER_HASH_MISMATCH")
    return json.loads(raw)


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _part_names(manifest: dict[str, Any], prefix: str) -> tuple[str, ...]:
    """按清单读取全部分片；保留旧资源包缺少技能描述第1片时的兼容。"""
    indices = []
    for item in manifest["files"]:
        match = re.fullmatch(re.escape(prefix) + r"\.([1-9][0-9]*)\.json", item["filename"])
        if match:
            indices.append(int(match[1]))
    first = 2 if prefix == "mappingData" and 1 not in indices else 1
    if not indices or len(indices) > 64 or sorted(indices) != list(range(first, max(indices) + 1)):
        raise CaptureError("MASTER_MANIFEST_INCOMPLETE")
    return tuple(f"{prefix}.{index}.json" for index in sorted(indices))


def _class_group(class_id: int) -> ClassGroup:
    # 按 Chaldea 职阶数据的 baseClassId 归并额外职阶与兽阶。
    base = class_id - 10000 if 10001 <= class_id <= 10007 else class_id
    if 13 <= class_id <= 19:
        base = class_id - 12
    if 1 <= base <= 7:
        return ("saber", "archer", "lancer", "rider", "caster", "assassin", "berserker")[base - 1]
    if class_id in {8, 9, 11, 23}:
        return "extra1"
    if class_id in {
        10,
        25,
        28,
        12,
        20,
        22,
        24,
        26,
        27,
        29,
        30,
        31,
        33,
        34,
        35,
        36,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
        44,
        9001,
        9002,
        9003,
        9004,
        10031,
        10033,
        10038,
        10040,
    }:
        return "extra2"
    return "unknown"


def _name(row: dict[str, Any], mappings: dict[str, Any], kind: str) -> str:
    original = str(row.get("name", ""))
    result = mappings.get(kind, {}).get(original, {}).get("CN") or original
    # 所有字符串都只是静态文本；渲染端仍须 HTML 转义。
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(result)).strip()[:160]


def _passive_overrides(row: dict[str, Any]) -> dict[int, tuple[int, ...]]:
    """复用 getAscended：灵基优先、灵衣次之，空列表不覆盖，双向解析灵衣ID。"""
    raw = row.get("ascensionAdd", {}).get("overwriteClassPassive", {})
    asc = {int(k): tuple(v) for k, v in raw.get("ascension", {}).items() if v}
    costume = {int(k): tuple(v) for k, v in raw.get("costume", {}).items() if v}
    result = dict(costume)
    result.update(asc)
    for detail in row.get("profile", {}).get("costume", {}).values():
        cid, battle = detail.get("id"), detail.get("battleCharaId")
        if isinstance(cid, int) and isinstance(battle, int):
            if cid not in result and battle in costume:
                result[cid] = costume[battle]
            if battle not in result and cid in costume:
                result[battle] = costume[cid]
    return result


def load_report_master(
    data_dir: Path, atlas_dir: Path, *, region: Literal["CN", "JP"] = "CN"
) -> ReportMasterData:
    """目录只能由本机资源模块提供；导入文件不能指定路径或清单。"""
    try:
        return _load(data_dir, atlas_dir, region)
    except CaptureError:
        raise
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        raise CaptureError("MASTER_DATA_INVALID") from None


def _load(data_dir: Path, atlas_dir: Path, region: Literal["CN", "JP"]) -> ReportMasterData:
    manifest = json.loads((data_dir / "SOURCE_MANIFEST.json").read_bytes())
    atlas_manifest = json.loads((atlas_dir / "SOURCE_MANIFEST.json").read_bytes())
    revision = manifest.get("data_revision", manifest.get("data_commit", ""))
    if (
        region not in {"CN", "JP"}
        or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision)
        or atlas_manifest["region"] != region
    ):
        raise CaptureError("MASTER_DATA_INVALID")
    mappings: dict[str, Any] = {}
    for filename in _part_names(manifest, "mappingData"):
        mappings.update(_read(data_dir, filename, manifest))
    _merge(mappings, _read(data_dir, "mappingPatch.json", manifest))
    released = set(mappings["entity_release"]["CN"]) if region == "CN" else set()
    wiki: dict[int, dict[str, Any]] = {}
    for filename in _part_names(manifest, "wiki.servants"):
        wiki.update({v["collectionNo"]: v for v in _read(data_dir, filename, manifest)})
    servants: dict[int, ServantMaster] = {}
    append_fallbacks: dict[int, int] = {}
    for filename in _part_names(manifest, "servants"):
        for row in _read(data_dir, filename, manifest):
            sid, collection = row["id"], row.get("collectionNo", 0)
            # 与 Servant 构造器/生成器默认值一致；不把 missing type 当未知角色。
            playable = collection > 0 and (
                row.get("type", "normal") in {"normal", "heroine"}
                or sid in {600710, 2501500, 1002100, 505700}
            )
            fallback = row.get("script", {}).get("transformInfo", {}).get("saveTransformDefault", 0)
            if fallback and fallback != sid and not row.get("appendPassive"):
                append_fallbacks[sid] = fallback
            extra = wiki.get(collection)
            obtains = frozenset(extra.get("obtains", [])) if extra is not None else None
            servants[sid] = ServantMaster(
                sid,
                _name(row, mappings, "svt_names"),
                row.get("rarity", 0),
                _class_group(row.get("classId", 0)),
                playable,
                region == "JP" or sid in released,
                obtains,
                row.get("classId"),
                tuple(v["id"] for v in row["classPassive"]) if "classPassive" in row else None,
                _passive_overrides(row),
                {v["num"]: v["skill"]["id"] for v in row.get("appendPassive", [])},
                {
                    num: tuple(v["id"] for v in row.get("skills", []) if v.get("num") == num)
                    for num in (1, 2, 3)
                },
            )
    for sid, fallback in append_fallbacks.items():
        if fallback in servants:
            servants[sid] = replace(
                servants[sid], append_skill_ids=dict(servants[fallback].append_skill_ids)
            )
    ces: dict[int, CraftEssenceMaster] = {}
    for filename in _part_names(manifest, "craftEssences"):
        for row in _read(data_dir, filename, manifest):
            sid = row["id"]
            ces[sid] = CraftEssenceMaster(
                sid,
                _name(row, mappings, "ce_names"),
                row.get("bondEquipOwner"),
                "svtEquipFriendShip" in row.get("flags", []),
                region == "JP" or sid in released,
            )
    gachas: dict[int, GachaMaster] = {}
    for filename in (
        ("mstGacha.json", "mstGachaCNExtra.json") if region == "CN" else ("mstGacha.json",)
    ):
        for row in _read(atlas_dir, filename, atlas_manifest):
            sid = row["id"]
            gachas[sid] = GachaMaster(
                sid,
                _name(row, mappings, "summon_names"),
                row["type"],
                row["openedAt"],
                row["closedAt"],
                row.get("imageId", 0),
            )
    quests: dict[int, QuestMaster] = {}
    for filename in _part_names(manifest, "wars"):
        for war in _read(data_dir, filename, manifest):
            parents = {war.get("parentWarId", 0), war.get("materialParentWarId", 0)}
            parents.update(
                a["overwriteId"]
                for a in war.get("warAdds", [])
                if a.get("type") in {"parentWar", "materialParentWar"}
            )
            for spot in war.get("spots", []):
                for row in spot.get("quests", []):
                    flags = set(row.get("flags", []))
                    repeat = row.get("afterClear") == "repeatLast"
                    raid = repeat and "raid" in flags
                    free = repeat and not flags.intersection(
                        {"noBattle", "dropFirstTimeOnly", "forceToNoDrop", "notRetrievable", "raid"}
                    )
                    main_free = (
                        row.get("type") == "free"
                        and repeat
                        and row.get("warId", 0) < 1000
                        and not flags.intersection({"forceToNoDrop", "dropFirstTimeOnly"})
                    )
                    group: QuestGroup = "other"
                    if raid:
                        group = "repeat_raid"
                    elif free:
                        group = (
                            "permanent_free"
                            if row.get("closedAt", 0) > 1800000000 or main_free or 8395 in parents
                            else "event_free"
                        )
                    ignore = (
                        row.get("warId") in {1002, 8123}
                        or "harvest" in flags
                        or (
                            len(row.get("phases", [])) == 1
                            and "noBattle" in flags
                            and row.get("consume", 0) == 0
                        )
                    )
                    sid = row["id"]
                    quests[sid] = QuestMaster(
                        sid, _name(row, mappings, "quest_names"), group, ignore, raid and not ignore
                    )
    shops = {r["id"]: r["shopType"] for r in _read(atlas_dir, "mstShop.json", atlas_manifest)}
    entities = _read(data_dir, "entities.json", manifest)
    entity_ids = frozenset(
        int(row["id"])
        for row in entities
        if row.get("type") is not None
        and row["type"] not in {"normal", "heroine"}
        and row["id"] not in {600710, 2501500, 1002100, 505700}
    )
    material_ids = frozenset(
        int(row["id"])
        for row in entities
        if row.get("type") in {"combineMaterial", "statusUp", "svtEquipMaterial", "svtMaterialTd"}
    )
    boards: dict[int, ClassBoardMaster] = {}
    grand_class_ids: dict[int, int] = {}
    # 兼容尚未下载星图字典的旧清单；缺项显式成为 needs_master_data。
    filenames = {v["filename"] for v in manifest["files"]}
    if "classBoards.json" in filenames:
        for row in _read(data_dir, "classBoards.json", manifest):
            bid = row["id"]
            names = {
                1: "Saber",
                2: "Archer",
                3: "Lancer",
                4: "Rider",
                5: "Caster",
                6: "Assassin",
                7: "Berserker",
                8: "EXTRA Ⅰ",
                9: "EXTRA Ⅱ",
            }
            base = bid - 10000 if bid > 10000 else bid
            grand = bid > 10000 or row.get("parentClassBoardBaseId", 0) > 0
            name = ("Grand " if grand else "") + names.get(base, str(bid))
            boards[bid] = ClassBoardMaster(
                bid,
                name,
                frozenset(c["classId"] for c in row.get("classes", [])),
                frozenset(
                    q["id"] for q in row.get("squares", []) if q.get("skillType", "none") != "none"
                ),
                frozenset(
                    q["id"] for q in row.get("squares", []) if q.get("lock", {}).get("items")
                ),
                frozenset(q["id"] for q in row.get("squares", [])),
                grand,
            )
    if "grandGraphs.json" in filenames:
        for row in _read(data_dir, "grandGraphs.json", manifest):
            for detail in row.get("details", []):
                base, grand_id = detail["baseClassId"], detail["grandClassId"]
                if base in grand_class_ids and grand_class_ids[base] != grand_id:
                    raise CaptureError("MASTER_DATA_INVALID")
                grand_class_ids[base] = grand_id
    skills: dict[int, SkillMaster] = {}
    class_icon_ids: dict[int, int] = {}
    grail_frame_types: dict[tuple[int, int], int] = {}
    if "baseSkills.json" in filenames:
        for row in _read(data_dir, "baseSkills.json", manifest):
            icon = row.get("icon", "")
            valid_icon = isinstance(icon, str) and bool(
                re.fullmatch(
                    r"https://static\.atlasacademy\.io/(?:JP|CN)/SkillIcons/[A-Za-z0-9_]+\.png",
                    icon,
                )
            )
            skills[row["id"]] = SkillMaster(
                row["id"],
                _name(row, mappings, "skill_names"),
                valid_icon,
                hashlib.sha256(icon.encode()).hexdigest() if valid_icon else None,
            )
    if "constData.json" in filenames:
        const = _read(data_dir, "constData.json", manifest)
        class_icon_ids = {
            int(key): value["iconImageId"]
            for key, value in const.get("classInfo", {}).items()
            if type(value.get("iconImageId")) is int and value["iconImageId"] > 0
        }
        frame_types = {
            "black": 0,
            "bronze": 1,
            "silver": 2,
            "gold": 3,
            "goldRed": 4,
            "goldRedGreat": 5,
            "goldBlack": 6,
            "goldBlackGreat": 7,
        }
        grail_frame_types = {
            (int(rarity), int(count)): frame_types[entry["frameType"]]
            for rarity, costs in const.get("svtGrailCost", {}).items()
            for count, entry in costs.items()
            if entry.get("frameType") in frame_types
        }
    version = _read(data_dir, "version.json", manifest)
    atlas_hash = str(atlas_manifest["atlas_info"]["hash"])
    return ReportMasterData(
        f"chaldea:{revision};atlas-{region.lower()}:{atlas_hash}",
        version["timestamp"],
        servants,
        ces,
        gachas,
        quests,
        shops,
        entity_ids,
        material_ids,
        boards,
        grand_class_ids,
        skills,
        class_icon_ids,
        grail_frame_types,
        region=region,
    )

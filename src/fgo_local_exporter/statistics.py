"""只导出统计所需的协议字段，原始响应和登录凭据不进入结果。"""

import base64
import gzip
import io
import json
import re
import urllib.parse
import zlib
from typing import Any

from fgo_local_exporter.errors import ExportError

__all__ = ["TABLE_FIELDS", "FIELD_ERROR_REASONS", "decode_response", "export_statistics"]
_CARD = (
    "id svtId lv exp limitCount adjustHp adjustAtk skillLv1 skillLv2 skillLv3 "
    "treasureDeviceLv1 exceedCount hp atk isLock status createdAt dispLimitCount transformVal"
)
_SUPPORT = (
    "svtId lv treasureDeviceLv skillLv1 skillLv2 skillLv3 grandSvt hp atk adjustHp "
    "adjustAtk skillId1 skillId2 skillId3 dispLimitCount exceedCount"
)
_FIELDS = {
    "userGame": "userId friendCode rkchannel name createdAt lv exp qp costMax actMax actRecoverAt "
    "carryOverActPoint freeStone chargeStone stone fixMainSupportDeckIds "
    "fixEventSupportDeckIds mainSupportDeckId eventSupportDeckId pushUserSvtId "
    "favoriteUserSvtId",
    "userItem": "itemId num",
    "userSvt": _CARD,
    "userSvtStorage": _CARD,
    "userSvtCollection": "svtId friendshipRank friendship friendshipExceedCount "
    "maxLv maxLimitCount "
    "skillLv1 skillLv2 skillLv3 treasureDeviceLv1 getNum totalGetNum costumeIds "
    "status",
    "userSvtCoin": "svtId num",
    "userSvtAppendPassiveSkill": "svtId unlockNums",
    "userSvtAppendPassiveSkillLv": "userSvtId appendPassiveSkillNums appendPassiveSkillLvs",
    "userEquip": "id equipId lv exp",
    "userCommandCode": "id commandCodeId",
    "userSvtCommandCode": "svtId userCommandCodeIds",
    "userSvtCommandCard": "svtId commandCardParam",
    "userClassBoardSquare": "classBoardBaseId classBoardSquareIds classBoardUnlockSquareIds",
    "userLogin": "totalLoginCount seqLoginCount",
    "userGacha": "gachaId num",
    "userQuest": "questId questPhase clearNum challengeNum updatedAt",
    "userShop": "shopId num",
    "userSupportDeck": "supportDeckId name",
    "userSvtLeader": "supportDeckId classId userSvtId ceSvtId ceLv ceLimitCount ceUserSvtId ceHp "
    "ceAtk appendSkillIds appendSkillLvs " + _SUPPORT,
    "userSvtGrand": "grandGraphId userSvtId equipTarget2SkillChange appendSkillIds appendSkillLvs "
    + _SUPPORT
    + " "
    + " ".join(
        prefix + suffix
        for prefix in ("ce", "ce2", "ce3")
        for suffix in ("SvtId", "UserSvtId", "Lv", "LimitCount", "Hp", "Atk")
    ),
}
TABLE_FIELDS = {name: frozenset(fields.split()) for name, fields in _FIELDS.items()}
_ARRAYS = frozenset(
    (
        "costumeIds fixMainSupportDeckIds fixEventSupportDeckIds unlockNums "
        "appendPassiveSkillNums appendPassiveSkillLvs userCommandCodeIds "
        "commandCardParam classBoardSquareIds classBoardUnlockSquareIds "
        "appendSkillIds appendSkillLvs"
    ).split()
)
_REQUIRED = {
    "userGame": "userId friendCode",
    "userItem": "itemId num",
    "userSvt": "id svtId",
    "userSvtStorage": "id svtId",
    "userSvtCollection": "svtId",
    "userSvtCoin": "svtId num",
    "userSvtAppendPassiveSkill": "svtId unlockNums",
    "userSvtAppendPassiveSkillLv": "userSvtId appendPassiveSkillNums appendPassiveSkillLvs",
    "userEquip": "id equipId",
    "userCommandCode": "id commandCodeId",
    "userSvtCommandCode": "svtId userCommandCodeIds",
    "userSvtCommandCard": "svtId commandCardParam",
    "userClassBoardSquare": "classBoardBaseId classBoardSquareIds classBoardUnlockSquareIds",
    "userGacha": "gachaId num",
    "userQuest": "questId",
    "userShop": "shopId num",
    "userSupportDeck": "supportDeckId",
    "userSvtLeader": "supportDeckId classId userSvtId svtId",
    "userSvtGrand": "grandGraphId userSvtId",
}
_SECRET = re.compile(r"usk|token|cookie|password|session|signature|authorization|access.?key", re.I)
_DECODE_LIMIT = 32 * 1024 * 1024


def _field_code(table, field):
    return "STAT_FIELD_" + re.sub(r"[^A-Za-z0-9]+", "_", table + "_" + field).upper()


_FIELD_PATHS = {(table, field) for table, fields in TABLE_FIELDS.items() for field in fields}
_FIELD_PATHS.update((table, "userId") for table in TABLE_FIELDS)
_FIELD_PATHS.add(("meta", "serverTime"))
for _table in ("userSvtLeader", "userSvtGrand"):
    for _slot in (1, 2, 3):
        _FIELD_PATHS.update(
            (_table, f"equipTarget{_slot}.{field}")
            for field in ("svtId", "userSvtId", "lv", "limitCount", "hp", "atk")
        )
    _FIELD_PATHS.update(
        (_table, field)
        for field in (
            "appendPassiveSkill",
            "appendPassiveSkill.skillId",
            "appendPassiveSkill.skillLv",
            "script",
            "script.equipTarget2SkillChange",
        )
    )
FIELD_ERROR_REASONS = {
    _field_code(table, field): f"统计字段 {table}.{field} 格式不兼容；未记录字段值"
    for table, field in _FIELD_PATHS
}
assert len(FIELD_ERROR_REASONS) == len(_FIELD_PATHS)
assert all(len(code) <= 64 for code in FIELD_ERROR_REASONS)


def _field_failure(table, field):
    code = _field_code(table, field)
    return ExportError(code if code in FIELD_ERROR_REASONS else "STATISTICS_FIELD_INVALID")


def _checked_field(table, field, value, *, number=False):
    try:
        return _number(value) if number else _safe_field(field, value)
    except ExportError as error:
        if error.code == "STATISTICS_FIELD_INVALID":
            raise _field_failure(table, field) from None
        raise


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ExportError("RESPONSE_DUPLICATE_FIELD")
        result[key] = value
    return result


def _limits(root):
    stack = [(root, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 1_000_000:
            raise ExportError("RESPONSE_LIMIT")
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)


def decode_response(raw: bytes) -> dict:
    try:
        for _ in range(5):
            if len(raw) > _DECODE_LIMIT:
                raise ExportError("RESPONSE_LIMIT")
            raw = raw.strip()
            if raw.startswith((b"{", b"\xef\xbb\xbf{")):
                root = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs)
                _limits(root)
                if not isinstance(root, dict):
                    raise ExportError("RESPONSE_INVALID")
                return root
            if raw.startswith(b"\x1f\x8b"):
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gzip_stream:
                    raw = gzip_stream.read(_DECODE_LIMIT + 1)
            elif raw.startswith((b"x\x01", b"x\x9c", b"x\xda")):
                stream = zlib.decompressobj()
                raw = stream.decompress(raw, _DECODE_LIMIT + 1)
                if not stream.eof:
                    raise ExportError("RESPONSE_LIMIT")
            elif b"%" in raw:
                raw = urllib.parse.unquote_to_bytes(raw)
            else:
                raw = base64.b64decode(raw, validate=True)
    except ExportError:
        raise
    except (ValueError, UnicodeError, OSError, RecursionError, zlib.error):
        raise ExportError("RESPONSE_ENCODING_INVALID") from None
    raise ExportError("RESPONSE_ENCODING_INVALID")


def _number(value):
    if type(value) not in (int, str) or not re.fullmatch(r"-?[0-9]{1,20}", str(value)):
        raise ExportError("STATISTICS_FIELD_INVALID")
    return value


def _safe_field(name, value):
    if name in _ARRAYS:
        if not isinstance(value, list) or len(value) > 1024:
            raise ExportError("STATISTICS_FIELD_INVALID")
        return [_number(v) for v in value]
    if name == "name":
        if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
            raise ExportError("STATISTICS_FIELD_INVALID")
        return value
    if name == "friendCode":
        if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9]{9}|[0-9]{12,20})", value):
            raise ExportError("STATISTICS_REGION_UNSUPPORTED")
        return value
    return _number(value)


def export_statistics(root: dict, *, allow_jp: bool = False) -> tuple[bytes, dict]:
    """导出成功登录的真实表。缺表保留缺失，未知字段永不通过。"""
    try:
        _limits(root)
        logins = [r for r in root["response"] if r.get("nid") == "login"]
        if (
            len(logins) != 1
            or str(logins[0].get("resCode")) not in {"0", "00"}
            or (logins[0].get("fail") or not isinstance(logins[0].get("success"), dict))
        ):
            raise ExportError("STATISTICS_LOGIN_REQUIRED")
        cache = root["cache"]
        replaced = cache["replaced"]
        games = replaced["userGame"]
        if not isinstance(games, list) or len(games) != 1:
            raise ExportError("STATISTICS_ACCOUNT_INVALID")
        account = str(_checked_field("userGame", "userId", games[0]["userId"]))
        if str(games[0].get("rkchannel")) == "1000" or "globalUser" in replaced:
            raise ExportError("STATISTICS_REGION_UNSUPPORTED")
        _safe_field("friendCode", games[0]["friendCode"])
        region = "JP" if len(games[0]["friendCode"]) == 9 else "CN"
        if region == "JP" and not allow_jp:
            raise ExportError("STATISTICS_REGION_UNSUPPORTED")
        server_time = _checked_field("meta", "serverTime", cache["serverTime"], number=True)
        if not 0 < int(server_time) <= 253402300799:
            raise _field_failure("meta", "serverTime")
        safe: dict[str, Any] = {
            "response": [{"nid": "login", "resCode": "00", "success": {}}],
            "cache": {"serverTime": server_time, "replaced": {}, "updated": {}},
        }
        counts = {}
        total = 0
        for table, names in TABLE_FIELDS.items():
            section = "replaced"
            if (
                table in {"userLogin", "userShop"}
                and table not in replaced
                and table in cache.get("updated", {})
                and (table == "userShop" or cache["updated"][table])
            ):
                # 商店累计记录保留原 updated 分区，解析器据此标明统计覆盖范围。
                section = "updated"
            if table not in cache.get(section, {}):
                continue
            rows = cache[section][table]
            if not isinstance(rows, list) or len(rows) > 100000:
                raise ExportError("STATISTICS_LIMIT")
            total += len(rows)
            if total > 100000:
                raise ExportError("STATISTICS_LIMIT")
            clean_rows = []
            for row in rows:
                if not isinstance(row, dict) or (
                    "userId" in row
                    and str(_checked_field(table, "userId", row["userId"], number=True)) != account
                ):
                    raise ExportError("STATISTICS_ACCOUNT_INVALID")
                if not set(_REQUIRED.get(table, "").split()).issubset(row):
                    raise ExportError("STATISTICS_REQUIRED_FIELD_MISSING")
                required = frozenset(_REQUIRED.get(table, "").split())
                # 可选空字段保留为缺失，不伪造零值；主键和关联字段仍严格校验。
                clean = {
                    name: _checked_field(table, name, value)
                    for name, value in row.items()
                    if name in names and (value is not None or name in required)
                }
                if table in {"userSvtLeader", "userSvtGrand"}:
                    # 嵌套字段只保留统计模型所支持的层级。
                    for slot in (1, 2, 3):
                        name = f"equipTarget{slot}"
                        if isinstance(row.get(name), dict):
                            clean[name] = {
                                key: _checked_field(table, name + "." + key, value, number=True)
                                for key, value in row[name].items()
                                if key in {"svtId", "userSvtId", "lv", "limitCount", "hp", "atk"}
                                and value is not None
                            }
                    if row.get("appendPassiveSkill") is not None:
                        append = row["appendPassiveSkill"]
                        if not isinstance(append, list) or len(append) > 10:
                            raise _field_failure(table, "appendPassiveSkill")
                        if any(
                            not isinstance(item, dict) or not {"skillId", "skillLv"}.issubset(item)
                            for item in append
                        ):
                            raise _field_failure(table, "appendPassiveSkill")
                        clean["appendPassiveSkill"] = [
                            {
                                name: _checked_field(
                                    table, "appendPassiveSkill." + name, item[name], number=True
                                )
                                for name in ("skillId", "skillLv")
                            }
                            for item in append
                        ]
                    if table == "userSvtGrand" and row.get("script") not in (None, []):
                        script = row["script"]
                        if not isinstance(script, dict):
                            raise _field_failure(table, "script")
                        if script.get("equipTarget2SkillChange") is not None:
                            clean["script"] = {
                                "equipTarget2SkillChange": _checked_field(
                                    table,
                                    "script.equipTarget2SkillChange",
                                    script["equipTarget2SkillChange"],
                                    number=True,
                                )
                            }
                clean_rows.append(clean)
            counts[table] = len(clean_rows)
            safe["cache"][section][table] = clean_rows
        if not {"userGame", "userSvt", "userSvtCollection"}.issubset(counts):
            raise ExportError("STATISTICS_CORE_TABLES_MISSING")
        pending: list[Any] = [safe]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if any(_SECRET.search(key) for key in value):
                    raise ExportError("STATISTICS_SECRET_FIELD_REJECTED")
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        body = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode()
        return body, {
            "region": region,
            "timezone": "Asia/Tokyo" if region == "JP" else "Asia/Shanghai",
            "name": games[0].get("name") or "未提供名称",
            "level": games[0].get("lv"),
            "captured_at": int(server_time),
            "tables": counts,
            "missing_tables": sorted(set(TABLE_FIELDS) - set(counts)),
            "bytes": len(body),
        }
    except ExportError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        raise ExportError("STATISTICS_STRUCTURE_INVALID") from None

"""登录响应白名单提取；日服由调用方明确启用，账号标识按区服隔离。"""

import hashlib
import hmac
import json
import re
from typing import Literal, cast

from fgo_stat_core.contracts.formation_name import validate_formation_name
from fgo_stat_core.contracts.master_name import validate_master_name
from fgo_stat_core.contracts.models import CaptureError, Limits, Snapshot, TableRows, Value
from fgo_stat_core.features.capture_import.decoder import decode_capture
from fgo_stat_core.features.capture_import.schema import (
    REPORT_SCHEMA_VERSION,
    REPORT_TABLES,
    SCHEMA_VERSION,
    TABLES,
    FieldSpec,
)

__all__ = ["parse_capture", "pseudonym", "snapshot_digest", "snapshot_json"]


def pseudonym(key: bytes, namespace: str, value: str) -> str:
    message = json.dumps([namespace, value], separators=(",", ":")).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _integer(value: object, *, signed: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CaptureError("FIELD_TYPE_INVALID")
    text = str(value)
    if not re.fullmatch(r"-?[0-9]{1,20}" if signed else r"[0-9]{1,20}", text):
        raise CaptureError("FIELD_VALUE_INVALID")
    return int(text)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CaptureError("STRUCTURE_INVALID")
    return cast(dict[str, object], value)


def _rows(value: object, limits: Limits) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise CaptureError("TABLE_INVALID")
    if len(value) > limits.max_rows:
        raise CaptureError("TOO_MANY_ROWS")
    return [_object(item) for item in value]


def _extract(spec: FieldSpec, value: object, key: bytes, account: str) -> Value:
    if spec.kind == "master_name":
        try:
            return validate_master_name(value)
        except ValueError:
            raise CaptureError("FIELD_VALUE_INVALID") from None
    if spec.kind == "formation_name":
        try:
            return validate_formation_name(value)
        except ValueError:
            raise CaptureError("FIELD_VALUE_INVALID") from None
    if spec.kind == "friend_code":
        if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9]{9}|[0-9]{12,20})", value):
            raise CaptureError("FIELD_VALUE_INVALID")
        return value
    if spec.kind in {"ints", "signed_ints", "code_refs"}:
        if not isinstance(value, list) or len(value) > 1024:
            raise CaptureError("FIELD_TYPE_INVALID")
        numbers = tuple(_integer(item, signed=spec.kind != "ints") for item in value)
        if spec.kind == "code_refs":
            if any(i < -1 for i in numbers):
                raise CaptureError("FIELD_VALUE_INVALID")
            return tuple(
                pseudonym(key, account + ":code", str(i)) if i > 1 else str(i) for i in numbers
            )
        return numbers
    number = _integer(value, signed=spec.kind == "signed")
    if spec.kind == "optional_card_ref":
        return pseudonym(key, account + ":card", str(number)) if number else 0
    if spec.kind in {"card_ref", "code_ref", "equip_ref"}:
        kind = {"card_ref": "card", "code_ref": "code", "equip_ref": "equip"}[spec.kind]
        return pseudonym(key, account + ":" + kind, str(number))
    return number


def _flatten_support(row: dict[str, object], *, grand: bool = False) -> dict[str, object]:
    # 只派生固定字段，嵌套 userId / 自由文本 / 装备未知字段不会跨过白名单。
    result = dict(row)
    # 仅冠位 script 的单个设置字段；输入自行伪造的同名扁平值无效。
    result.pop("equipTarget2SkillChange", None)
    if grand and row.get("script") is not None:
        script = _object(row["script"])
        if "equipTarget2SkillChange" in script:
            result["equipTarget2SkillChange"] = script["equipTarget2SkillChange"]
    for name in ("appendSkillIds", "appendSkillLvs"):
        result.pop(name, None)
    for position, prefix in ((1, "ce"), (2, "ce2"), (3, "ce3")):
        for suffix in ("SvtId", "UserSvtId", "Lv", "LimitCount", "Hp", "Atk"):
            result.pop(prefix + suffix, None)
        equip = row.get(f"equipTarget{position}")
        if equip is not None:
            target = _object(equip)
            for source, suffix in (
                ("svtId", "SvtId"),
                ("userSvtId", "UserSvtId"),
                ("lv", "Lv"),
                ("limitCount", "LimitCount"),
                ("hp", "Hp"),
                ("atk", "Atk"),
            ):
                if source in target:
                    result[prefix + suffix] = target[source]
    append = row.get("appendPassiveSkill")
    if append is not None:
        if not isinstance(append, list) or len(append) > 10:
            raise CaptureError("FIELD_TYPE_INVALID")
        entries = [_object(v) for v in append]
        if any("skillId" not in v or "skillLv" not in v for v in entries):
            raise CaptureError("REQUIRED_FIELD_MISSING")
        result["appendSkillIds"] = [v["skillId"] for v in entries]
        result["appendSkillLvs"] = [v["skillLv"] for v in entries]
    return result


def parse_capture(
    raw: bytes, key: bytes, limits: Limits | None = None, *, report_fields: bool = False,
    allow_jp: bool = False,
) -> Snapshot:
    if len(key) < 32:
        raise CaptureError("CONFIG_INVALID")
    bounds = limits or Limits()
    table_specs = REPORT_TABLES if report_fields else TABLES
    root = decode_capture(raw, bounds)
    responses = root.get("response")
    if not isinstance(responses, list) or not responses:
        raise CaptureError("LOGIN_RESPONSE_REQUIRED")
    successful = False
    login_count = 0
    for response in responses:
        row = _object(response)
        if row.get("nid") == "login":
            login_count += 1
            code = row.get("resCode")
            if type(code) not in (str, int) or code not in ("0", "00", 0) or row.get("fail"):
                raise CaptureError("LOGIN_FAILED")
            if not isinstance(row.get("success"), dict):
                raise CaptureError("LOGIN_FAILED")
            successful = True
    if not successful:
        raise CaptureError("LOGIN_RESPONSE_REQUIRED")
    if login_count != 1:
        raise CaptureError("LOGIN_RESPONSE_AMBIGUOUS")
    cache = _object(root.get("cache"))
    replaced = _object(cache.get("replaced"))
    updated = _object(cache.get("updated", {}))
    timestamp = _integer(cache.get("serverTime"))
    if timestamp < 1 or timestamp > 253402300799:
        raise CaptureError("TIMESTAMP_INVALID")
    games = _rows(replaced.get("userGame"), bounds)
    if len(games) != 1:
        raise CaptureError("ACCOUNT_AMBIGUOUS")
    game = games[0]
    # 与固定版 Chaldea 区服判定保持一致；不保存这些判定原值。
    friend_code = game.get("friendCode")
    if (
        not isinstance(friend_code, str)
        or not friend_code.isascii()
        or not friend_code.isdigit()
        or not re.fullmatch(r"(?:[0-9]{9}|[0-9]{12,20})", friend_code)
        or str(game.get("rkchannel")) == "1000"
        or "globalUser" in replaced
    ):
        raise CaptureError("REGION_UNSUPPORTED")
    region: Literal["CN", "JP"] = "JP" if len(friend_code) == 9 else "CN"
    if region == "JP" and not allow_jp:
        raise CaptureError("REGION_UNSUPPORTED")
    account_id = str(_integer(game.get("userId")))
    account = pseudonym(key, "game:" + region, account_id)
    tables: dict[str, TableRows] = {}
    warnings: list[str] = ["ENTITY_CLASSIFICATION_PENDING_STATIC_DATA"]
    total_rows = 0
    for table_name, spec in table_specs.items():
        source = replaced
        if report_fields and table_name in {"userLogin", "userShop"} and table_name not in replaced:
            if table_name in updated and (table_name == "userShop" or updated[table_name]):
                source = updated
                if table_name == "userShop":
                    # 兑换次数是逐商店累计值；保留已有记录，不把增量表当作完整历史。
                    warnings.append("INCREMENTAL_TABLE_USED:userShop")
        if table_name not in source:
            if table_name in updated:
                warnings.append("INCREMENTAL_TABLE_SKIPPED:" + table_name)
            continue
        input_rows = _rows(source[table_name], bounds)
        if table_name == "userLogin" and len(input_rows) != 1:
            raise CaptureError("ACCOUNT_AMBIGUOUS")
        total_rows += len(input_rows)
        if total_rows > bounds.max_rows:
            raise CaptureError("TOO_MANY_ROWS")
        output_rows: list[dict[str, Value]] = []
        keys: set[Value] = set()
        for input_row in input_rows:
            if "userId" in input_row and str(_integer(input_row["userId"])) != account_id:
                raise CaptureError("ACCOUNT_MISMATCH")
            if report_fields and table_name in {"userSvtLeader", "userSvtGrand"}:
                input_row = _flatten_support(input_row, grand=table_name == "userSvtGrand")
            output_row: dict[str, Value] = {}
            if table_name in {"userGame", "userLogin"}:
                output_row["account_key"] = account
            for field in spec.fields:
                if field.name not in input_row:
                    if field.required:
                        raise CaptureError("REQUIRED_FIELD_MISSING")
                    continue
                output_row[field.name] = _extract(field, input_row[field.name], key, account)
            if table_name == "userSvtLeader":
                output_row["slot_key"] = f"{output_row['supportDeckId']}:{output_row['classId']}"
            primary = output_row[spec.key]
            if primary in keys:
                raise CaptureError("DUPLICATE_PRIMARY_KEY")
            keys.add(primary)
            if table_name == "userSvtAppendPassiveSkillLv":
                nums = cast(tuple[int, ...], output_row["appendPassiveSkillNums"])
                levels = cast(tuple[int, ...], output_row["appendPassiveSkillLvs"])
                if len(nums) != len(levels) or len(set(nums)) != len(nums):
                    raise CaptureError("APPEND_ARRAY_INVALID")
            output_rows.append(output_row)
        tables[table_name] = tuple(output_rows)
    if len(tables) <= 1:
        raise CaptureError("SNAPSHOT_DATA_REQUIRED")
    instance_ids = {r["id"] for t in ("userSvt", "userSvtStorage") for r in tables.get(t, ())}
    orphan_count = sum(
        r["userSvtId"] not in instance_ids for r in tables.get("userSvtAppendPassiveSkillLv", ())
    )
    if orphan_count:
        warnings.append("APPEND_REFERENCE_UNRESOLVED")
    missing = set(table_specs) - set(tables)
    if missing:
        warnings.append("MISSING_TABLES_PRESERVED")
    return Snapshot(
        (REPORT_SCHEMA_VERSION if report_fields else SCHEMA_VERSION)
        if region == "CN" else "jp_capture.v2" if report_fields else "jp_capture.v1",
        region,
        account,
        timestamp,
        tables,
        tuple(warnings),
        {name: timestamp for name in tables},
    )


def snapshot_json(snapshot: Snapshot) -> str:
    return json.dumps(
        {
            "schema_version": snapshot.schema_version,
            "region": snapshot.region,
            "account_key": snapshot.account_key,
            "captured_at": snapshot.captured_at,
            "tables": snapshot.tables,
            "warnings": snapshot.warnings,
            "table_times": snapshot.table_times,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def snapshot_digest(snapshot: Snapshot) -> str:
    return hashlib.sha256(snapshot_json(snapshot).encode()).hexdigest()

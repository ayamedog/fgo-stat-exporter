"""按区服主数据匹配卡池累计抽数；排序、筛选与分页由共享模块处理。"""

from typing import Any, Literal, cast

from fgo_stat_core.contracts.gacha_records import GachaHistory, GachaRecord
from fgo_stat_core.contracts.models import CaptureError, Snapshot, Value
from fgo_stat_core.contracts.report_master import ReportMasterData

__all__ = ["build_gacha_history", "gacha_page"]


def _count(row: dict[str, Value], field: str) -> int | None:
    value = row.get(field)
    return value if type(value) is int and value >= 0 else None


def _time(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 253402300799 else None


def _default_order(record: GachaRecord) -> tuple[int, int, int]:
    if record.gacha_id <= 100 or record.kind == "friend":
        return 0, record.gacha_id, 0
    # 长期开放的剧情、新手等卡池单独置顶，不让累计次数影响时间顺序。
    if (
        record.opened_at is not None
        and record.closed_at is not None
        and record.closed_at - record.opened_at >= 365 * 86400
    ):
        return 1, -record.opened_at, record.gacha_id
    return 2, -(record.opened_at or 0), -record.gacha_id


def build_gacha_history(snapshot: Snapshot, master: ReportMasterData) -> GachaHistory:
    if snapshot.region != master.region:
        raise CaptureError("MASTER_REGION_MISMATCH")
    rows = snapshot.tables.get("userGacha", ())
    if len(rows) > 10_000:
        raise CaptureError("DETAILS_LIMIT")
    records = []
    for row in rows:
        gid, pulls = _count(row, "gachaId"), _count(row, "num")
        if not gid or pulls is None:
            raise CaptureError("DETAILS_DATA_INVALID")
        pool = master.gachas.get(gid)
        kind = {1: "stone", 3: "friend", 5: "ticket", 7: "lucky"}.get(
            pool.type if pool else 0, "unknown"
        )
        records.append(
            GachaRecord(
                gacha_id=gid,
                pulls=pulls,
                name=pool.name if pool else None,
                kind=cast(Literal["stone", "friend", "lucky", "ticket", "unknown"], kind),
                opened_at=_time(pool.opened_at) if pool else None,
                closed_at=_time(pool.closed_at) if pool else None,
                image_id=pool.image_id if pool and pool.image_id > 0 else None,
            )
        )
    return GachaHistory(
        region=snapshot.region,
        timezone="Asia/Tokyo" if snapshot.region == "JP" else "Asia/Shanghai",
        captured_at=snapshot.captured_at,
        available="userGacha" in snapshot.tables,
        records=tuple(sorted(records, key=_default_order)),
    )


def gacha_page(
    history: GachaHistory,
    *,
    query: str = "",
    kind: str = "all",
    sort: str = "default",
    page: int = 1,
) -> dict[str, Any]:
    if (
        len(query) > 100
        or type(page) is not int
        or not 1 <= page <= 100_000
        or sort not in {"default", "recent", "pulls"}
        or kind not in {"all", "stone", "friend", "lucky", "ticket", "unknown"}
    ):
        raise CaptureError("DETAILS_QUERY_INVALID")
    needle = query.strip().casefold()
    records = [
        row
        for row in history.records
        if (kind == "all" or row.kind == kind)
        and (needle in (row.name or "").casefold() or needle in str(row.gacha_id))
    ]
    if sort == "recent":
        records.sort(key=lambda row: (-(row.opened_at or 0), -row.gacha_id))
    elif sort == "pulls":
        records.sort(key=lambda row: (-row.pulls, row.gacha_id))
    pages = max(1, (len(records) + 29) // 30)
    page = min(page, pages)
    return {
        "region": history.region,
        "timezone": history.timezone,
        "captured_at": history.captured_at,
        "available": history.available,
        "page": page,
        "pages": pages,
        "total": len(records),
        "summary": {"pulls": sum(row.pulls for row in records)},
        "rows": [row.model_dump(mode="json") for row in records[(page - 1) * 30 : page * 30]],
    }

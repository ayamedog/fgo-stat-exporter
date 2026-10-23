"""日服导入与双区隔离使用人工响应，不读取外部账号文件。"""

import json
from dataclasses import replace

import pytest

from fgo_local_exporter.mobile import bounded_import
from fgo_local_exporter.statistics import export_statistics
from fgo_stat_core.contracts.models import CaptureError
from fgo_stat_core.contracts.report import (
    GrandCraftEssenceSlot,
    GrandSupportEntry,
    Metric,
    SupportSlot,
)
from fgo_stat_core.contracts.report_master import ReportMasterData
from fgo_stat_core.features.capture_import.parser import parse_capture
from fgo_stat_core.features.report.calculator import build_report
from fgo_stat_core.infrastructure.native_support_adapter import native_panels
from fgo_stat_core.infrastructure.report_ui.base_blocks import resources


def artificial(minimal_capture, *, region="JP"):
    replaced = minimal_capture["cache"]["replaced"]
    replaced["userGame"][0].update(
        friendCode="000000001" if region == "JP" else "000000000001",
        createdAt=1700000000,
        name="人工双区验收",
    )
    replaced.update(userSvt=[], userSvtCollection=[], userItem=[{"itemId": 17, "num": 5}])
    minimal_capture["cache"]["updated"] = {
        "userShop": [
            {"shopId": 401, "num": 3, "token": "ARTIFICIAL_EXCLUDED_SECRET"},
            {"shopId": 402, "num": 4},
            {"shopId": 403, "num": 8},
        ]
    }
    return minimal_capture


@pytest.mark.parametrize("region", ["CN", "JP"])
def test_mobile_shop_updates_survive_export_reimport_and_rendering(
    minimal_capture, hmac_key, region
):
    root = artificial(minimal_capture, region=region)
    body, preview = bounded_import(json.dumps(root).encode())
    assert preview["region"] == region and b"ARTIFICIAL_EXCLUDED_SECRET" not in body
    assert "userShop" not in json.loads(body)["cache"]["replaced"]
    again, _ = bounded_import(body)
    assert again == body
    snapshot = parse_capture(body, hmac_key, report_fields=True, allow_jp=True)
    master = ReportMasterData(
        "artificial.v1",
        snapshot.captured_at,
        region=region,
        shop_types={401: 7, 402: 7, 403: 1},
    )
    report = build_report(snapshot, master, generated_at=snapshot.captured_at)
    metric = next(
        m for m in report.resources if m.value and m.value.resource == "unregistered_spirit"
    )
    assert metric.value.exchange_count == 7 and metric.value.current_inventory == 5
    assert metric.value.represented_total == 75
    assert "PARTIAL_SOURCE" in metric.issues
    html = resources({"resources": [metric.model_dump(mode="json")]})
    assert '<span class="sum">75</span>' in html
    if region == "JP":
        with pytest.raises(CaptureError, match="MASTER_REGION_MISMATCH"):
            build_report(snapshot, replace(master, region="CN"), generated_at=snapshot.captured_at)


def test_identical_account_numbers_have_different_region_pseudonyms(minimal_capture, hmac_key):
    root = artificial(minimal_capture)
    raw, _ = export_statistics(root, allow_jp=True)
    jp = parse_capture(raw, hmac_key, report_fields=True, allow_jp=True)
    root["cache"]["replaced"]["userGame"][0]["friendCode"] = "000000000001"
    raw, _ = export_statistics(root)
    cn = parse_capture(raw, hmac_key, report_fields=True)
    assert jp.account_key != cn.account_key
    assert jp.region == "JP" and cn.region == "CN"


def test_grand_keeps_eleven_columns_and_servants_actual_classes(minimal_capture, hmac_key):
    raw, _ = bounded_import(json.dumps(artificial(minimal_capture)).encode())
    snapshot = parse_capture(raw, hmac_key, report_fields=True, allow_jp=True)
    report = build_report(snapshot, None, generated_at=snapshot.captured_at)
    entries = tuple(
        GrandSupportEntry(
            graph_id=graph,
            servant=SupportSlot(
                position="extra",
                state="ready",
                servant_id=101,
                rarity=5,
                level=120,
                np_level=5,
                skill_levels=(10, 10, 10),
                grand=True,
                frame_type_base=3,
                grand_class_icon_key=f"class-3-{icon}.png",
                craft_essence_state="empty",
            ),
            craft_essences=tuple(
                GrandCraftEssenceSlot(position=n, state="empty") for n in (1, 2, 3)
            ),
        )
        for graph, icon in ((10010, 10023), (10011, 10010))
    )
    metric = Metric(
        state="ready", value=entries, rule_id="artificial", source_tables=("userSvtGrand",)
    )
    report = report.model_copy(update={"supports": None, "grand_supports": metric})
    panel = native_panels(report)[0]
    assert len(panel.slots) == 11
    assert panel.slots[9]["class_icon"] == "class3_10023"
    assert panel.slots[10]["class_icon"] == "class3_10010"
    assert all(row["state"] == "unavailable" for row in panel.slots[:9])


def test_missing_shop_remains_unknown(minimal_capture, hmac_key):
    root = artificial(minimal_capture)
    root["cache"]["updated"].pop("userShop")
    raw, _ = bounded_import(json.dumps(root).encode())
    snapshot = parse_capture(raw, hmac_key, report_fields=True, allow_jp=True)
    master = ReportMasterData("artificial.v1", snapshot.captured_at, region="JP")
    report = build_report(snapshot, master, generated_at=snapshot.captured_at)
    value = next(m.value for m in report.resources if m.value.resource == "unregistered_spirit")
    assert value.exchange_count is None and value.represented_total is None
    assert value.current_inventory == 5

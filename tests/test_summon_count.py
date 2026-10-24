"""人工数据验证扣除前、扣除福袋保底后的两种比例。"""

import pytest

from fgo_stat_core.contracts.models import Snapshot
from fgo_stat_core.contracts.report_master import GachaMaster, ReportMasterData, ServantMaster
from fgo_stat_core.features.report.calculator import build_report


def test_total_pulls_include_lucky_bag_and_exclude_friend_points():
    master = ReportMasterData(
        "artificial",
        1792670000,
        servants={
            100100: ServantMaster(100100, "人工五星", 5, "saber", True, True, frozenset({"gacha"}))
        },
        gachas={
            1: GachaMaster(1, "人工友情池", 3, 1, 2, 0),
            1001: GachaMaster(1001, "人工石池", 1, 1, 2, 0),
            2001: GachaMaster(2001, "人工福袋", 7, 1, 2, 0),
        },
    )
    snapshot = Snapshot(
        "artificial",
        "CN",
        "artificial-account",
        1792670000,
        {
            "userSvtCollection": ({"svtId": 100100, "status": 2, "totalGetNum": 19},),
            "userGacha": (
                {"gachaId": 1, "num": 50},
                {"gachaId": 1001, "num": 123},
                {"gachaId": 2001, "num": 11},
            ),
        },
    )
    metric = build_report(snapshot, master, generated_at=1792670000).summon_estimate
    assert metric.value.adjusted_pulls == 134
    assert metric.value.adjusted_ssr_acquisitions == 18
    assert metric.value.lucky_bag_records == 1
    assert metric.value.ssr_acquisitions_before_guarantee == 19
    assert metric.value.pulls_after_guarantee == 133
    assert metric.value.rate_before_guarantee == pytest.approx(19 / 134 * 100)
    assert metric.value.rate_after_guarantee == pytest.approx(18 / 133 * 100)
    assert metric.rule_id == "cn-summon.v3"


def summon_metric(*, region="CN", count=120, pulls=10000, bags=10, bag_pulls=11):
    master = ReportMasterData(
        "artificial",
        1792670000,
        servants={
            100100: ServantMaster(100100, "人工五星", 5, "saber", True, True, frozenset({"gacha"}))
        },
        gachas={
            1001: GachaMaster(1001, "人工石池", 1, 1, 2, 0),
            **{
                2001 + i: GachaMaster(2001 + i, "人工福袋", 7, 1, 2, 0)
                for i in range(bags)
            },
        },
        region=region,
    )
    snapshot = Snapshot(
        "artificial", region, "artificial-account", 1792670000,
        {
            "userSvtCollection": ({"svtId": 100100, "status": 2, "totalGetNum": count},),
            "userGacha": (
                {"gachaId": 1001, "num": pulls - bags * bag_pulls},
                *({"gachaId": 2001 + i, "num": bag_pulls} for i in range(bags)),
            ),
        },
    )
    return build_report(snapshot, master, generated_at=1792670000).summon_estimate


@pytest.mark.parametrize("region", ["CN", "JP"])
@pytest.mark.parametrize("bags", [0, 1, 2, 10])
def test_two_rates_preserve_total_pulls_and_deduct_only_guaranteed_draw(region, bags):
    metric = summon_metric(region=region, bags=bags)
    value = metric.value
    assert metric.rule_id == region.lower() + "-summon.v3"
    assert value.adjusted_pulls == 10000
    assert value.ssr_acquisitions_before_guarantee == 120
    assert value.adjusted_ssr_acquisitions == 120 - bags
    assert value.pulls_after_guarantee == 10000 - bags
    assert value.rate_before_guarantee == pytest.approx(1.2)
    assert value.rate_after_guarantee == pytest.approx((120 - bags) / (10000 - bags) * 100)


def test_zero_acquisitions_are_a_valid_zero_rate():
    value = summon_metric(count=0, pulls=100, bags=0).value
    assert value.rate_before_guarantee == value.rate_after_guarantee == 0.0


def test_only_guaranteed_draw_keeps_before_rate_and_marks_after_unavailable():
    metric = summon_metric(count=1, pulls=1, bags=1, bag_pulls=1)
    assert metric.value.rate_before_guarantee == 100.0
    assert metric.value.pulls_after_guarantee == 0
    assert metric.value.rate_after_guarantee is None
    assert "PARTIAL_SOURCE" in metric.issues
    assert "NaN" not in metric.model_dump_json() and "Infinity" not in metric.model_dump_json()


def test_inconsistent_bag_records_do_not_produce_negative_or_infinite_after_rate():
    metric = summon_metric(count=10, pulls=1, bags=3, bag_pulls=0)
    assert metric.value.rate_before_guarantee == 1000.0
    assert metric.value.pulls_after_guarantee is None
    assert metric.value.rate_after_guarantee is None
    assert "PARTIAL_SOURCE" in metric.issues


def test_ui_uses_the_two_calculated_rates_with_after_rate_in_parentheses():
    from fgo_stat_core.infrastructure.report_ui.base_blocks import summon

    html = summon({"summon_estimate": summon_metric().model_dump(mode="json")})
    assert '1.20<span class="unit">%</span><span class="rate-after">(1.10%)</span>' in html
    assert "含福袋（不含福袋）" in html
    assert '<span class="number">+10</span><span class="text">福袋记录</span>' in html
    assert "按图鉴累计获得数与卡池累计抽数估算" in html
    assert "括号外：" not in html and "每次保底扣除" not in html

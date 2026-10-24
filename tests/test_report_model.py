"""报告仅约束已核对口径；没有计算器和真实账号输出。"""

from datetime import date

import pytest
from pydantic import ValidationError

from fgo_stat_core.contracts.report import (
    AccountOverview,
    AnnualReport,
    FavoriteServant,
    GachaRankings,
    Metric,
    ReportMetadata,
    ResourceBreakdown,
    SummonEstimate,
)


@pytest.mark.parametrize("state", ["ready", "partial"])
def test_available_state_requires_value(state: str) -> None:
    with pytest.raises(ValidationError, match="REPORT_VALUE_REQUIRED"):
        Metric[int](state=state, source_tables=("userGame",), rule_id="account.v1")


@pytest.mark.parametrize("state", ["needs_master_data", "unavailable"])
@pytest.mark.parametrize("value", [0, 1])
def test_missing_state_cannot_fabricate_zero_or_any_value(state: str, value: int) -> None:
    with pytest.raises(ValidationError, match="REPORT_UNAVAILABLE_VALUE_FORBIDDEN"):
        Metric[int](
            state=state,
            value=value,
            issues=("MASTER_DATA_MISSING",),
            source_tables=("userGame",),
            rule_id="account.v1",
        )


def test_partial_requires_explanation() -> None:
    with pytest.raises(ValidationError, match="REPORT_REASON_REQUIRED"):
        Metric[int](state="partial", value=3, source_tables=("userGame",), rule_id="account.v1")


def test_zero_is_valid_when_data_really_ready() -> None:
    metric = Metric[int](state="ready", value=0, source_tables=("userGame",), rule_id="account.v1")
    assert metric.value == 0


@pytest.mark.parametrize("field", ["userId", "friendCode", "usk", "signature_url", "name"])
def test_raw_identity_and_secrets_are_not_report_metadata(field: str) -> None:
    values = dict(
        captured_at=1800000000,
        generated_at=1800000001,
        as_of_day=date(2027, 1, 15),
        selected_year=2027,
    )
    values[field] = "SYNTHETIC_ONLY_SECRET"
    with pytest.raises(ValidationError) as caught:
        ReportMetadata.model_validate(values)
    assert "SYNTHETIC_ONLY_SECRET" not in str(caught.value)


@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5, float("nan"), float("inf")])
def test_summon_denominator_is_strict_positive_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        SummonEstimate(
            adjusted_ssr_acquisitions=0,
            adjusted_pulls=value,
            lucky_bag_records=0,
            unmapped_gacha_records=0,
        )


@pytest.mark.parametrize("field", ["rate_before_guarantee", "rate_after_guarantee"])
@pytest.mark.parametrize("value", [-1.0, True, float("nan"), float("inf"), -float("inf")])
def test_summon_rates_reject_invalid_or_non_finite_values(field, value):
    with pytest.raises(ValidationError):
        SummonEstimate(
            adjusted_ssr_acquisitions=0,
            adjusted_pulls=100,
            lucky_bag_records=0,
            unmapped_gacha_records=0,
            **{field: value},
        )


@pytest.mark.parametrize("key", ["https://host/image", "../private", "/private", "a/../b", "a\\b"])
def test_asset_reference_is_not_a_url_or_traversal_path(key: str) -> None:
    with pytest.raises(ValidationError):
        FavoriteServant(roles=("push",), servant_id=501001, level=1, np_level=1, asset_key=key)


def test_report_scope_labels_are_explicit() -> None:
    ranks = GachaRankings(all_time=(), selected_pool_year=())
    assert ranks.year_scope == "pool_open_or_close_year"
    resource = ResourceBreakdown(resource="great_lantern")
    assert resource.represented_consumed is None
    assert resource.current_inventory is None
    assert resource.represented_total is None
    assert resource.coverage == "snapshot_derived_not_lifetime_ledger"


def test_missing_report_serializes_null_without_rendering_any_account_values() -> None:
    def unavailable(rule: str, table: str):
        return dict(
            state="unavailable",
            value=None,
            issues=("CAPTURE_FIELD_MISSING",),
            source_tables=(table,),
            rule_id=rule,
        )

    report = AnnualReport.model_validate(
        {
            "metadata": dict(
                captured_at=1800000000,
                generated_at=1800000001,
                as_of_day=date(2027, 1, 15),
                selected_year=2027,
            ),
            "account": unavailable("account.v1", "userGame"),
            "favorites": unavailable("favorites.v1", "userGame"),
            "collection": unavailable("collection.v1", "userSvtCollection"),
            "summon_estimate": unavailable("summon.v1", "userGacha"),
            "bond": unavailable("bond.v1", "userSvt"),
            "gachas": unavailable("gachas.v1", "userGacha"),
            "quests": unavailable("quests.v1", "userQuest"),
            "resources": (),
        }
    )
    assert report.model_dump()["account"]["value"] is None
    assert report.model_dump_json().count('"value":null') == 7
    assert str(report) == "<AnnualReport>"
    assert repr(report) == "<AnnualReport>"


def test_account_summary_does_not_accept_account_identifiers() -> None:
    account = AccountOverview(created_day=date(2020, 1, 1), tenure_days=20, total_login_days=15)
    assert "2020" not in repr(account)
    assert account.friend_code is None
    assert set(account.model_dump()) == {
        "created_day",
        "tenure_days",
        "total_login_days",
        "consecutive_login_days",
        "friend_code",
        "master_name",
    }

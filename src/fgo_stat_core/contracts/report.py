"""经 Chaldea 公式核对的报告输出契约；不计算、不渲染、不接收原始抓包。"""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from fgo_stat_core.contracts.formation_name import validate_formation_name
from fgo_stat_core.contracts.master_name import validate_master_name

__all__ = [
    "REPORT_VERSION",
    "AccountOverview",
    "AnnualReport",
    "BondHistory",
    "CollectionSummary",
    "FavoriteServant",
    "GachaEntry",
    "GachaRankings",
    "Metric",
    "QuestEntry",
    "QuestRankings",
    "RarityCount",
    "ReportMetadata",
    "ResourceBreakdown",
    "SummonEstimate",
    "YearCount",
]

REPORT_VERSION: Literal["cn_report.v1"] = "cn_report.v1"
type Count = Annotated[int, Field(ge=0)]
type EntityId = Annotated[int, Field(gt=0)]
type Timestamp = Annotated[int, Field(gt=0, le=253402300799)]
type Year = Annotated[int, Field(ge=2015, le=2200)]
type PublicName = Annotated[str, Field(max_length=160)]
type Issue = Literal[
    "CAPTURE_FIELD_MISSING",
    "MASTER_DATA_MISSING",
    "MASTER_DATA_STALE",
    "PARTIAL_SOURCE",
    "UNKNOWN_GACHA",
    "UNCERTAIN_HISTORICAL_RULE",
    "UPSTREAM_DISPLAY_OFFSET_EXCLUDED",
    "SNAPSHOT_TIME_MIXED",
    "ESTIMATED_FROM_CUMULATIVE_TOTALS",
    "NOT_STRICT_CALENDAR_YEAR",
]
type TableName = Literal[
    "userGame",
    "userLogin",
    "userSvt",
    "userSvtStorage",
    "userSvtCollection",
    "userSvtGrand",
    "userSvtAppendPassiveSkillLv",
    "userGacha",
    "userQuest",
    "userShop",
    "userItem",
    "userSvtLeader",
    "userSupportDeck",
    "userClassBoardSquare",
]


class _SafeModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    def __str__(self) -> str:
        return repr(self)


class Metric[T](_SafeModel):
    state: Literal["ready", "partial", "needs_master_data", "unavailable"]
    value: T | None = None
    issues: tuple[Issue, ...] = ()
    source_tables: tuple[TableName, ...]
    rule_id: Annotated[str, Field(pattern=r"^[a-z0-9_.:-]{1,100}$")]

    @model_validator(mode="after")
    def check_availability(self) -> Self:
        if self.state in {"ready", "partial"} and self.value is None:
            raise ValueError("REPORT_VALUE_REQUIRED")
        if self.state in {"needs_master_data", "unavailable"} and self.value is not None:
            raise ValueError("REPORT_UNAVAILABLE_VALUE_FORBIDDEN")
        if self.state != "ready" and not self.issues:
            raise ValueError("REPORT_REASON_REQUIRED")
        return self


class ReportMetadata(_SafeModel):
    schema_version: Literal["cn_report.v1", "jp_report.v1"] = REPORT_VERSION
    region: Literal["CN", "JP"] = "CN"
    captured_at: Timestamp
    generated_at: Timestamp
    as_of_day: date
    selected_year: Year
    timezone: Literal["Asia/Shanghai", "Asia/Tokyo"] = "Asia/Shanghai"
    formula_reference: Literal["chaldea:24ac568a64f3741e4ddd0c17810e0ac338d772ad"] = (
        "chaldea:24ac568a64f3741e4ddd0c17810e0ac338d772ad"
    )
    master_data_revision: Annotated[str, Field(max_length=100)] | None = None


class AccountOverview(_SafeModel):
    created_day: date
    tenure_days: Count
    total_login_days: Count | None = None
    consecutive_login_days: Count | None = None
    # 仅来自已确认的 v2 userGame.name；缺失保留 None，不从 appname 等字段兜底。
    master_name: Annotated[str, AfterValidator(validate_master_name)] | None = None
    # 仅取用户选择显示的 friendCode；数字可能与 userId 相同，禁止从后者兜底。
    friend_code: Annotated[str, Field(pattern=r"^(?:[0-9]{9}|[0-9]{12,20})$")] | None = None


class FavoriteServant(_SafeModel):
    roles: tuple[Literal["push", "favorite"], ...]
    servant_id: EntityId
    name: PublicName | None = None
    level: Count
    np_level: Count
    bond_level: Count | None = None
    bond_cap: Count | None = None
    grand: bool | None = None
    # 仅静态资源键；模型故意不提供任意 URL 或原始实例标识字段。
    asset_key: (
        Annotated[
            str,
            Field(
                pattern=r"^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*(?:\.[a-zA-Z0-9]+)?$", max_length=160
            ),
        ]
        | None
    ) = None


class RarityCount(_SafeModel):
    rarity: Annotated[int, Field(ge=0, le=5)]
    owned: Count
    released: Count


class CollectionSummary(_SafeModel):
    owned_types: Count
    released_types: Count
    owned_ssr_types: Count
    current_ssr_np_levels: Count
    by_rarity: tuple[RarityCount, ...]
    # 固定公共分组键由主数据适配生成；不接受账号字符串。
    by_class: dict[
        Literal[
            "saber",
            "archer",
            "lancer",
            "rider",
            "caster",
            "assassin",
            "berserker",
            "extra1",
            "extra2",
            "unknown",
        ],
        Count,
    ]


class SummonEstimate(_SafeModel):
    adjusted_ssr_acquisitions: Count
    # 保留字段名以兼容既有报告；此抽数仅排除友情点，包含福袋。
    adjusted_pulls: Annotated[int, Field(gt=0)]
    lucky_bag_records: Count
    unmapped_gacha_records: Count
    estimated_from_cumulative_totals: Literal[True] = True


class YearCount(_SafeModel):
    year: Year
    count: Count


class BondHistory(_SafeModel):
    currently_held_bond_ces: Count
    eligible_bond_ce_types: Count
    current_year_held_bond_ces: Count
    bond_15_servants: Count
    by_acquired_year: tuple[YearCount, ...]
    history_scope: Literal["currently_held_instances"] = "currently_held_instances"


class GachaEntry(_SafeModel):
    gacha_id: EntityId
    cumulative_pulls: Count
    name: PublicName | None = None
    opened_at: Timestamp | None = None
    closed_at: Timestamp | None = None
    banner_image_id: Count | None = None


class GachaRankings(_SafeModel):
    all_time: tuple[GachaEntry, ...]
    selected_pool_year: tuple[GachaEntry, ...]
    year_scope: Literal["pool_open_or_close_year"] = "pool_open_or_close_year"


class QuestEntry(_SafeModel):
    quest_id: EntityId
    count: Count
    name: PublicName | None = None


class QuestRankings(_SafeModel):
    permanent_free: tuple[QuestEntry, ...]
    event_free: tuple[QuestEntry, ...]
    repeat_raid: tuple[QuestEntry, ...]
    failed_attempts: tuple[QuestEntry, ...]
    coverage: Literal["snapshot_records_only"] = "snapshot_records_only"


class ResourceBreakdown(_SafeModel):
    resource: Literal["lantern", "great_lantern", "grail", "crystal", "unregistered_spirit"]
    represented_consumed: Count | None = None
    current_inventory: Count | None = None
    represented_total: Count | None = None
    active_skill_consumed: Count | None = None
    append_skill_consumed: Count | None = None
    exchange_count: Count | None = None
    coverage: Literal["snapshot_derived_not_lifetime_ledger"] = (
        "snapshot_derived_not_lifetime_ledger"
    )


class SupportCraftEssence(_SafeModel):
    craft_essence_id: EntityId
    name: PublicName | None = None
    level: Count
    limit_break_count: Count
    # 服务端已按礼装等级计算的白值；突破影响等级上限，不重复加成。
    hp: Count | None = None
    atk: Count | None = None


class SupportParameters(_SafeModel):
    context: Literal["normal_support", "grand_configuration"]
    servant_hp: Count | None = None  # 服务端从者值，已包含芙芙，不含冠位/礼装。
    servant_atk: Count | None = None
    base_hp: Count | None = None  # 当前等级基础值 = servant_hp - fou_hp。
    base_atk: Count | None = None
    fou_hp: Count | None = None
    fou_atk: Count | None = None
    grand_bonus: Literal[0, 1000] | None = None
    equipment_hp: Count | None = None
    equipment_atk: Count | None = None
    hp: Count | None = None
    atk: Count | None = None
    scope: Literal["pre_battle_without_skill_effects"] = "pre_battle_without_skill_effects"

    @model_validator(mode="after")
    def validate_components(self) -> Self:
        for stat in ("hp", "atk"):
            servant = getattr(self, "servant_" + stat)
            fou = getattr(self, "fou_" + stat)
            base = getattr(self, "base_" + stat)
            equip = getattr(self, "equipment_" + stat)
            total = getattr(self, stat)
            if base is not None and (servant is None or fou is None or base + fou != servant):
                raise ValueError("SUPPORT_PARAMETER_COMPONENTS_INVALID")
            if total is not None and (
                servant is None
                or self.grand_bonus is None
                or equip is None
                or total != servant + self.grand_bonus + equip
                or (fou is not None and fou > servant)
            ):
                raise ValueError("SUPPORT_PARAMETER_TOTAL_INVALID")
        return self


class ClassScoreProgress(_SafeModel):
    board_id: EntityId
    board_name: PublicName
    enhanced_nodes: Count
    total_nodes: Count
    unlocked_locks: Count
    total_locks: Count
    # 节点记录不等于全部职阶效果已满足开放条件。
    coverage: Literal["recorded_nodes_not_effect_activation"] = (
        "recorded_nodes_not_effect_activation"
    )

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.enhanced_nodes > self.total_nodes or self.unlocked_locks > self.total_locks:
            raise ValueError("CLASS_SCORE_COUNTS_INVALID")
        return self


class SupportSkill(_SafeModel):
    skill_id: EntityId
    name: PublicName | None = None
    icon_key: Annotated[str, Field(pattern=r"^skill-[1-9][0-9]*\.png$")] | None = None
    level: Count | None = None
    kind: Literal["active", "class_passive"]

    @model_validator(mode="after")
    def validate_skill(self) -> Self:
        if self.icon_key is not None and self.icon_key != f"skill-{self.skill_id}.png":
            raise ValueError("SKILL_ICON_ID_MISMATCH")
        if self.kind == "class_passive" and self.level is not None:
            raise ValueError("PASSIVE_HAS_NO_UPGRADE_LEVEL")
        return self


class SupportActiveIcon(_SafeModel):
    position: Literal[1, 2, 3]
    source: Literal["captured_id", "shared_static_icon", "unavailable"]
    icon_key: (
        Annotated[
            str, Field(pattern=r"^(?:skill-[1-9][0-9]*|active-shared-[1-9][0-9]*-[1-3])\.png$")
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def validate_icon(self) -> Self:
        if (self.source == "unavailable") != (self.icon_key is None):
            raise ValueError("ACTIVE_ICON_STATE_INVALID")
        if self.icon_key and self.icon_key.startswith("active-shared-"):
            if self.source != "shared_static_icon" or not self.icon_key.endswith(
                f"-{self.position}.png"
            ):
                raise ValueError("ACTIVE_ICON_SOURCE_INVALID")
        elif self.icon_key and self.source != "captured_id":
            raise ValueError("ACTIVE_ICON_SOURCE_INVALID")
        return self


type ActiveIcons = tuple[SupportActiveIcon, SupportActiveIcon, SupportActiveIcon]


class SupportAppendSkill(_SafeModel):
    num: Literal[100, 101, 102, 103, 104]
    state: Literal["unlocked", "locked", "unavailable"]
    skill_id: EntityId | None = None
    name: PublicName | None = None
    icon_key: Annotated[str, Field(pattern=r"^skill-[1-9][0-9]*\.png$")] | None = None
    level: Annotated[int, Field(ge=1, le=10)] | None = None

    @model_validator(mode="after")
    def validate_append(self) -> Self:
        if self.icon_key is not None and self.icon_key != f"skill-{self.skill_id}.png":
            raise ValueError("SKILL_ICON_ID_MISMATCH")
        if (self.state == "unlocked") != (self.level is not None):
            raise ValueError("APPEND_LEVEL_STATE_INVALID")
        return self


type AppendSkills = tuple[
    SupportAppendSkill,
    SupportAppendSkill,
    SupportAppendSkill,
    SupportAppendSkill,
    SupportAppendSkill,
]


class SupportSlot(_SafeModel):
    position: Literal[
        "all", "saber", "archer", "lancer", "rider", "caster", "assassin", "berserker", "extra"
    ]
    state: Literal["ready", "empty", "unavailable"]
    servant_id: EntityId | None = None
    rarity: Annotated[int, Field(ge=0, le=5)] | None = None
    name: PublicName | None = None
    level: Count | None = None
    np_level: Count | None = None
    skill_levels: tuple[Count, Count, Count] | None = None
    append_skill_ids: tuple[EntityId, ...] | None = None
    append_skill_levels: tuple[Count, ...] | None = None
    craft_essence: SupportCraftEssence | None = None
    craft_essence_state: Literal["ready", "empty", "unavailable"] | None = None
    grand: bool | None = None
    class_score: Metric[ClassScoreProgress] | None = None
    grand_score: Metric[ClassScoreProgress] | None = None
    parameters: Metric[SupportParameters] | None = None
    active_skills: (
        Metric[tuple[SupportSkill | None, SupportSkill | None, SupportSkill | None]] | None
    ) = None
    passive_skills: Metric[tuple[SupportSkill, ...]] | None = None
    passive_scope: Literal["class_passive_only"] = "class_passive_only"
    append_skills: Metric[AppendSkills] | None = None
    active_skill_icons: Metric[ActiveIcons] | None = None
    class_id: EntityId | None = None
    grand_class_id: EntityId | None = None
    class_icon_key: Annotated[str, Field(pattern=r"^class-[0-3]-[1-9][0-9]*\.png$")] | None = None
    grand_class_icon_key: Annotated[str, Field(pattern=r"^class-3-[1-9][0-9]*\.png$")] | None = None
    # constData.svtGrailCost 的基础框型；不推测 Great 动态升格。
    frame_type_base: Literal[0, 1, 2, 3, 4, 5, 6, 7] | None = None

    @model_validator(mode="after")
    def validate_slot(self) -> Self:
        if self.active_skill_icons and self.active_skill_icons.value is not None:
            if tuple(v.position for v in self.active_skill_icons.value) != (1, 2, 3):
                raise ValueError("ACTIVE_THREE_POSITIONS_REQUIRED")
        if self.append_skills and self.append_skills.value is not None:
            if tuple(v.num for v in self.append_skills.value) != (100, 101, 102, 103, 104):
                raise ValueError("APPEND_FIVE_POSITIONS_REQUIRED")
        if self.craft_essence_state is not None and (self.craft_essence_state == "ready") != (
            self.craft_essence is not None
        ):
            raise ValueError("SUPPORT_EQUIPMENT_STATE_INVALID")
        if self.state == "ready" and any(
            v is None for v in (self.servant_id, self.level, self.np_level, self.skill_levels)
        ):
            raise ValueError("SUPPORT_DETAILS_REQUIRED")
        if self.state != "ready" and any(
            v is not None
            for v in (
                self.servant_id,
                self.name,
                self.level,
                self.np_level,
                self.skill_levels,
                self.craft_essence,
                self.parameters,
            )
        ):
            raise ValueError("SUPPORT_EMPTY_DETAILS_FORBIDDEN")
        if (self.append_skill_ids is None) != (self.append_skill_levels is None):
            raise ValueError("SUPPORT_APPEND_LENGTH_INVALID")
        if len(self.append_skill_ids or ()) != len(self.append_skill_levels or ()):
            raise ValueError("SUPPORT_APPEND_LENGTH_INVALID")
        return self


class SupportGroup(_SafeModel):
    kind: Literal["normal", "event"]
    index: Annotated[int, Field(ge=1, le=10)]
    # 仅 userSupportDeck.supportDeckId 精确匹配；None 缺失，空串为明确空名。
    name: Annotated[str, AfterValidator(validate_formation_name)] | None = None
    # 仅旧单组字段的指向；None 表示不能以旧字段判断，不能称唯一生效队伍。
    legacy_selected: bool | None = None
    slots: tuple[SupportSlot, ...]

    @model_validator(mode="after")
    def validate_positions(self) -> Self:
        if len(self.slots) != 9 or len({s.position for s in self.slots}) != 9:
            raise ValueError("SUPPORT_NINE_POSITIONS_REQUIRED")
        return self


class GrandCraftEssenceSlot(_SafeModel):
    position: Literal[1, 2, 3]
    state: Literal["ready", "empty", "unavailable"]
    craft_essence: SupportCraftEssence | None = None
    # 仅第二礼装的明确设置；缺字段/未知数值不采用上游的缺省0。
    effect_mode: Literal["original", "starting_np_50"] | None = None

    @model_validator(mode="after")
    def validate_equipment(self) -> Self:
        if (self.state == "ready") != (self.craft_essence is not None):
            raise ValueError("GRAND_EQUIPMENT_STATE_INVALID")
        if self.effect_mode is not None and (self.position != 2 or self.state != "ready"):
            raise ValueError("GRAND_EQUIPMENT_EFFECT_POSITION_INVALID")
        return self


class GrandSupportEntry(_SafeModel):
    graph_id: EntityId
    servant: SupportSlot
    craft_essences: tuple[GrandCraftEssenceSlot, ...]
    coverage: Literal["grand_configuration_not_public_availability"] = (
        "grand_configuration_not_public_availability"
    )

    @model_validator(mode="after")
    def validate_equipment_positions(self) -> Self:
        if tuple(v.position for v in self.craft_essences) != (1, 2, 3):
            raise ValueError("GRAND_THREE_EQUIPMENT_POSITIONS_REQUIRED")
        return self


class AnnualReport(_SafeModel):
    metadata: ReportMetadata
    account: Metric[AccountOverview]
    favorites: Metric[tuple[FavoriteServant, ...]]
    collection: Metric[CollectionSummary]
    summon_estimate: Metric[SummonEstimate]
    bond: Metric[BondHistory]
    gachas: Metric[GachaRankings]
    quests: Metric[QuestRankings]
    resources: tuple[Metric[ResourceBreakdown], ...]
    supports: Metric[tuple[SupportGroup, ...]] | None = None
    grand_supports: Metric[tuple[GrandSupportEntry, ...]] | None = None

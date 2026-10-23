"""报告所需的公共主数据子集；不含用户数据或任意下载 URL。"""

from dataclasses import dataclass, field
from typing import Literal

type ClassGroup = Literal[
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
]
type QuestGroup = Literal["permanent_free", "event_free", "repeat_raid", "other"]


@dataclass(frozen=True)
class ServantMaster:
    id: int
    name: str
    rarity: int
    class_group: ClassGroup
    playable: bool
    released: bool
    obtains: frozenset[str] | None
    class_id: int | None = None
    passive_skill_ids: tuple[int, ...] | None = None
    passive_overrides: dict[int, tuple[int, ...]] = field(default_factory=dict)
    append_skill_ids: dict[int, int] = field(default_factory=dict)
    active_skill_ids: dict[int, tuple[int, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillMaster:
    id: int
    name: str
    icon_available: bool
    icon_signature: str | None = None


@dataclass(frozen=True)
class CraftEssenceMaster:
    id: int
    name: str
    bond_owner: int | None
    bond: bool
    released: bool


@dataclass(frozen=True)
class GachaMaster:
    id: int
    name: str
    type: int
    opened_at: int
    closed_at: int
    image_id: int = 0


@dataclass(frozen=True)
class QuestMaster:
    id: int
    name: str
    group: QuestGroup
    ignore_failed: bool
    # 上游存在账户特定历史补丁；通用报告不带入账号号码或假定适用。
    uncertain_raid_history: bool = False


@dataclass(frozen=True)
class ClassBoardMaster:
    id: int
    name: str
    class_ids: frozenset[int]
    skill_square_ids: frozenset[int]
    lock_square_ids: frozenset[int]
    all_square_ids: frozenset[int]
    grand: bool


@dataclass(frozen=True)
class ReportMasterData:
    revision: str
    timestamp: int
    servants: dict[int, ServantMaster] = field(default_factory=dict)
    craft_essences: dict[int, CraftEssenceMaster] = field(default_factory=dict)
    gachas: dict[int, GachaMaster] = field(default_factory=dict)
    quests: dict[int, QuestMaster] = field(default_factory=dict)
    shop_types: dict[int, int] = field(default_factory=dict)
    known_nonservant_ids: frozenset[int] = frozenset()
    known_material_ids: frozenset[int] = frozenset()
    class_boards: dict[int, ClassBoardMaster] = field(default_factory=dict)
    grand_class_ids: dict[int, int] = field(default_factory=dict)
    skills: dict[int, SkillMaster] = field(default_factory=dict)
    class_icon_ids: dict[int, int] = field(default_factory=dict)
    grail_frame_types: dict[tuple[int, int], int] = field(default_factory=dict)
    region: Literal["CN", "JP"] = "CN"

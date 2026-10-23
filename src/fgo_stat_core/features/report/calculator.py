"""Chaldea 固定版统计口径的 Python 实现；只消费白名单，无网络/数据库/SDK。"""

from collections import Counter
from datetime import date, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from fgo_stat_core.contracts.models import CaptureError, Snapshot, TableRows, Value
from fgo_stat_core.contracts.report import (
    AccountOverview,
    AnnualReport,
    BondHistory,
    ClassScoreProgress,
    CollectionSummary,
    FavoriteServant,
    GachaEntry,
    GachaRankings,
    GrandCraftEssenceSlot,
    GrandSupportEntry,
    Issue,
    Metric,
    QuestEntry,
    QuestRankings,
    RarityCount,
    ReportMetadata,
    ResourceBreakdown,
    SummonEstimate,
    SupportCraftEssence,
    SupportGroup,
    SupportSlot,
    TableName,
    YearCount,
)
from fgo_stat_core.contracts.report_master import ReportMasterData
from fgo_stat_core.features.report.parameters import support_parameters
from fgo_stat_core.features.report.skills import (
    active_skill_icons,
    append_skills,
    class_icon_key,
    frame_type_base,
)

__all__ = ["build_report"]
_TIMEZONES = {"CN": "Asia/Shanghai", "JP": "Asia/Tokyo"}
_CARD_TABLES: tuple[TableName, ...] = ("userSvt", "userSvtStorage")


class _Missing(Exception):
    pass


def _int(row: dict[str, Value], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise _Missing
    return value


def _date(timestamp: int, region: str = "CN") -> date:
    if timestamp <= 0 or timestamp > 253402300799:
        raise _Missing
    try:
        return datetime.fromtimestamp(timestamp, ZoneInfo(_TIMEZONES[region])).date()
    except (ValueError, OverflowError):
        raise _Missing from None


class _Report:
    def __init__(
        self,
        snapshot: Snapshot,
        master: ReportMasterData | None,
        year: int,
        day: date,
        show_friend_code: bool,
    ):
        self.snapshot, self.master, self.year, self.day = snapshot, master, year, day
        self.show_friend_code = show_friend_code
        for name, limit in (
            ("userSvtGrand", 20),
            ("userSvtLeader", 1000),
            ("userSupportDeck", 100),
            ("userClassBoardSquare", 100),
        ):
            if len(snapshot.tables.get(name, ())) > limit:
                raise CaptureError("REPORT_INPUT_LIMIT")
        self.crown_members = {r["userSvtId"] for r in snapshot.tables.get("userSvtGrand", ())}
        self.crown_flags: dict[Value, set[Value]] = {}
        for name in ("userSvtGrand", "userSvtLeader"):
            for row in snapshot.tables.get(name, ()):
                if "userSvtId" in row and "grandSvt" in row:
                    self.crown_flags.setdefault(row["userSvtId"], set()).add(row["grandSvt"])

    def local_date(self, timestamp: int) -> date:
        return _date(timestamp, self.snapshot.region)

    def rows(self, table: TableName, fields: tuple[str, ...] = ()) -> TableRows:
        if table not in self.snapshot.tables:
            raise _Missing
        rows = self.snapshot.tables[table]
        if any(any(key not in row for key in fields) for row in rows):
            raise _Missing
        return rows

    def metric[T](
        self,
        value: T | None,
        sources: tuple[TableName, ...],
        rule: str,
        issues: tuple[Issue, ...] = (),
        *,
        needs_master: bool = False,
    ) -> Metric[T]:
        if value is not None and self.master is not None:
            if "userSvtCollection" in sources and self.has_unknown_owned():
                issues += ("MASTER_DATA_MISSING",)
            if rule in {"collection", "bond"} and self.has_unknown_cards():
                issues += ("MASTER_DATA_MISSING",)
            static_sources = {
                "userSvtCollection",
                "userGacha",
                "userQuest",
                "userShop",
                "userSvtGrand",
                "userClassBoardSquare",
            }
            if (
                static_sources.intersection(sources)
                and self.master.timestamp < self.snapshot.captured_at
            ):
                issues += ("MASTER_DATA_STALE",)
        issues = tuple(dict.fromkeys(issues))
        times = {
            self.snapshot.table_times.get(t, self.snapshot.captured_at)
            for t in sources
            if t in self.snapshot.tables
        }
        if len(times) > 1:
            issues += ("SNAPSHOT_TIME_MIXED",)
        return Metric(
            state=("needs_master_data" if needs_master else "unavailable")
            if value is None
            else ("partial" if issues else "ready"),
            value=value,
            issues=issues
            or (("MASTER_DATA_MISSING",) if needs_master else ("CAPTURE_FIELD_MISSING",))
            if value is None
            else issues,
            source_tables=sources,
            rule_id=self.snapshot.region.lower() + "-summon.v2"
            if rule == "summon" else "chaldea24." + rule,
        )

    def has_unknown_cards(self) -> bool:
        if self.master is None:
            return True
        classified = (
            self.master.servants.keys()
            | self.master.craft_essences.keys()
            | self.master.known_material_ids
        )
        return any(
            row.get("svtId") not in classified
            for table in _CARD_TABLES
            for row in self.snapshot.tables.get(table, ())
        )

    def has_unknown_owned(self) -> bool:
        if self.master is None:
            return True
        known = (
            self.master.servants.keys()
            | self.master.craft_essences.keys()
            | self.master.known_nonservant_ids
        )
        return any(
            row.get("status") == 2 and row.get("svtId") not in known
            for row in self.snapshot.tables.get("userSvtCollection", ())
        )

    def owned(self) -> dict[int, dict[str, Value]]:
        assert self.master is not None
        return {
            _int(r, "svtId"): r
            for r in self.rows("userSvtCollection", ("svtId", "status"))
            if _int(r, "status") == 2
            and (svt := self.master.servants.get(_int(r, "svtId"))) is not None
            and svt.playable
            and svt.released
        }

    def account(self) -> Metric[AccountOverview]:
        sources: tuple[TableName, ...] = ("userGame", "userLogin")
        try:
            games = self.rows("userGame", ("createdAt",))
            if len(games) != 1:
                raise _Missing
            created = self.local_date(_int(games[0], "createdAt"))
            if created > self.day:
                raise _Missing
            issues: tuple[Issue, ...] = ()
            login: int | None = None
            consecutive: int | None = None
            try:
                rows = self.rows("userLogin", ("totalLoginCount",))
                if len(rows) != 1:
                    raise _Missing
                login = _int(rows[0], "totalLoginCount")
                if "seqLoginCount" in rows[0]:
                    consecutive = _int(rows[0], "seqLoginCount")
            except _Missing:
                issues = ("CAPTURE_FIELD_MISSING",)
            # Chaldea 的午夜截止时间减创建时刻后 ceil，与本区日历日期差等价。
            value = AccountOverview(
                created_day=created,
                tenure_days=(self.day - created).days,
                total_login_days=login,
                consecutive_login_days=consecutive,
                master_name=cast(str, games[0]["name"]) if "name" in games[0] else None,
                friend_code=cast(str, games[0]["friendCode"])
                if self.show_friend_code and "friendCode" in games[0]
                else None,
            )
            return self.metric(value, sources, "account", issues)
        except _Missing:
            return self.metric(None, sources, "account")

    def favorites(self) -> Metric[tuple[FavoriteServant, ...]]:
        sources: tuple[TableName, ...] = (
            "userGame",
            *_CARD_TABLES,
            "userSvtCollection",
            "userSvtGrand",
            "userSvtLeader",
        )
        try:
            game = self.rows("userGame", ("pushUserSvtId", "favoriteUserSvtId"))[0]
            cards = {r["id"]: r for t in _CARD_TABLES for r in self.rows(t)}
            collections = {r["svtId"]: r for r in self.snapshot.tables.get("userSvtCollection", ())}
            selected: dict[str, list[Literal["push", "favorite"]]] = {}
            issues: list[Issue] = []
            for field, role in (("pushUserSvtId", "push"), ("favoriteUserSvtId", "favorite")):
                ref = game[field]
                if ref == 0:
                    continue
                if not isinstance(ref, str) or ref not in cards:
                    issues.append("PARTIAL_SOURCE")
                    continue
                selected.setdefault(ref, []).append(cast(Literal["push", "favorite"], role))
            result: list[FavoriteServant] = []
            for ref, roles in selected.items():
                row = cards[ref]
                sid = _int(row, "svtId")
                collection = collections.get(sid)
                bond = (
                    _int(collection, "friendshipRank")
                    if collection and "friendshipRank" in collection
                    else None
                )
                cap = (
                    _int(collection, "friendshipExceedCount") + (5 if sid == 800100 else 10)
                    if collection and "friendshipExceedCount" in collection
                    else None
                )
                static = self.master.servants.get(sid) if self.master else None
                grand = self.support_grand_flag({"userSvtId": ref}, issues)
                if static is None:
                    issues.append("MASTER_DATA_MISSING")
                if bond is None or cap is None or grand is None:
                    issues.append("CAPTURE_FIELD_MISSING")
                result.append(
                    FavoriteServant(
                        roles=tuple(roles),
                        servant_id=sid,
                        name=static.name if static else None,
                        level=_int(row, "lv"),
                        np_level=_int(row, "treasureDeviceLv1"),
                        bond_level=bond,
                        bond_cap=cap,
                        grand=grand,
                    )
                )
            return self.metric(tuple(result), sources, "favorites", tuple(issues))
        except (IndexError, _Missing):
            return self.metric(None, sources, "favorites")

    def collection(self) -> Metric[CollectionSummary]:
        sources: tuple[TableName, ...] = ("userSvtCollection", *_CARD_TABLES)
        if self.master is None:
            return self.metric(None, sources, "collection", needs_master=True)
        try:
            owned = self.owned()
            svts = self.master.servants
            available = [s for s in svts.values() if s.playable and s.released]
            counts = Counter(svts[sid].rarity for sid in owned)
            all_counts = Counter(s.rarity for s in available)
            np = sum(
                _int(r, "treasureDeviceLv1")
                for t in _CARD_TABLES
                for r in self.rows(t)
                if (s := svts.get(_int(r, "svtId"))) and s.playable and s.rarity == 5
            )
            unknown = any(
                _int(r, "svtId")
                not in (
                    svts.keys()
                    | self.master.craft_essences.keys()
                    | self.master.known_nonservant_ids
                )
                and _int(r, "status") == 2
                for r in self.rows("userSvtCollection")
            )
            result = CollectionSummary(
                owned_types=len(owned),
                released_types=len(available),
                owned_ssr_types=counts[5],
                current_ssr_np_levels=np,
                by_rarity=tuple(
                    RarityCount(rarity=k, owned=counts[k], released=all_counts[k])
                    for k in sorted(all_counts)
                ),
                by_class=dict(Counter(svts[sid].class_group for sid in owned)),
            )
            return self.metric(
                result, sources, "collection", ("MASTER_DATA_MISSING",) if unknown else ()
            )
        except _Missing:
            return self.metric(None, sources, "collection")

    def bond(self) -> Metric[BondHistory]:
        sources: tuple[TableName, ...] = ("userSvtCollection", *_CARD_TABLES)
        if self.master is None:
            return self.metric(None, sources, "bond", needs_master=True)
        try:
            owned = self.owned()
            ces = {
                sid: ce for sid, ce in self.master.craft_essences.items() if ce.bond and ce.released
            }
            years = Counter(
                self.local_date(_int(r, "createdAt")).year
                for t in _CARD_TABLES
                for r in self.rows(t)
                if _int(r, "svtId") in ces
            )
            result = BondHistory(
                currently_held_bond_ces=sum(years.values()),
                eligible_bond_ce_types=sum(ce.bond_owner in owned for ce in ces.values()),
                current_year_held_bond_ces=years[self.year],
                bond_15_servants=sum(_int(r, "friendshipRank") >= 15 for r in owned.values()),
                by_acquired_year=tuple(
                    YearCount(year=y, count=n) for y, n in sorted(years.items())
                ),
            )
            return self.metric(result, sources, "bond")
        except _Missing:
            return self.metric(None, sources, "bond")

    def stone_gachas(self) -> tuple[list[dict[str, Value]], int]:
        assert self.master is not None
        rows = [
            r
            for r in self.rows("userGacha", ("gachaId", "num"))
            if _int(r, "gachaId") >= 100
            and ((g := self.master.gachas.get(_int(r, "gachaId"))) is None or g.type != 3)
        ]
        return rows, sum(_int(r, "gachaId") not in self.master.gachas for r in rows)

    def summon(self) -> Metric[SummonEstimate]:
        sources: tuple[TableName, ...] = ("userSvtCollection", "userGacha")
        if self.master is None:
            return self.metric(None, sources, "summon", needs_master=True)
        try:
            owned = self.owned()
            if self.has_unknown_owned():
                return self.metric(None, sources, "summon", needs_master=True)
            rows, unknown = self.stone_gachas()
            bags = sum(
                (g := self.master.gachas.get(_int(r, "gachaId"))) is not None and g.type == 7
                for r in rows
            )
            count = 0
            for sid, row in owned.items():
                svt = self.master.servants[sid]
                if svt.rarity != 5 or sid in {800100, 2801200}:
                    continue
                if svt.obtains is None:
                    return self.metric(None, sources, "summon", needs_master=True)
                if not svt.obtains.intersection({"eventReward", "clearReward", "unavailable"}):
                    count += _int(row, "totalGetNum")
            # 总抽数保留福袋，只有五星次数扣除福袋保底。
            pulls = sum(_int(r, "num") for r in rows)
            count -= bags
            if count < 0 or pulls <= 0:
                return self.metric(None, sources, "summon", ("PARTIAL_SOURCE",))
            value = SummonEstimate(
                adjusted_ssr_acquisitions=count,
                adjusted_pulls=pulls,
                lucky_bag_records=bags,
                unmapped_gacha_records=unknown,
            )
            issues: tuple[Issue, ...] = ("ESTIMATED_FROM_CUMULATIVE_TOTALS",)
            if unknown:
                issues += ("UNKNOWN_GACHA",)
            return self.metric(value, sources, "summon", issues)
        except _Missing:
            return self.metric(None, sources, "summon")

    def gachas(self) -> Metric[GachaRankings]:
        sources: tuple[TableName, ...] = ("userGacha",)
        if self.master is None:
            return self.metric(None, sources, "gachas", needs_master=True)
        try:
            rows, unknown = self.stone_gachas()
            all_time, selected = [], []
            for row in sorted(rows, key=lambda r: -_int(r, "num")):
                sid = _int(row, "gachaId")
                master = self.master.gachas.get(sid)
                entry = GachaEntry(
                    gacha_id=sid,
                    cumulative_pulls=_int(row, "num"),
                    name=master.name if master else None,
                    opened_at=master.opened_at if master and master.opened_at > 0 else None,
                    closed_at=master.closed_at if master and master.closed_at > 0 else None,
                    banner_image_id=master.image_id if master and master.image_id > 0 else None,
                )
                all_time.append(entry)
                if (
                    master
                    and master.opened_at > 0
                    and master.closed_at > 0
                    and master.closed_at - master.opened_at < 365 * 86400
                    and self.year in {
                        self.local_date(master.opened_at).year,
                        self.local_date(master.closed_at).year,
                    }
                ):
                    selected.append(entry)
            issues: tuple[Issue, ...] = ("NOT_STRICT_CALENDAR_YEAR",)
            if unknown:
                issues += ("UNKNOWN_GACHA",)
            return self.metric(
                GachaRankings(
                    all_time=tuple(all_time[:100]), selected_pool_year=tuple(selected[:100])
                ),
                sources,
                "gachas",
                issues,
            )
        except _Missing:
            return self.metric(None, sources, "gachas")

    def quests(self) -> Metric[QuestRankings]:
        sources: tuple[TableName, ...] = ("userQuest",)
        if self.master is None:
            return self.metric(None, sources, "quests", needs_master=True)
        try:
            buckets: dict[str, list[tuple[int, int, int, str]]] = {
                key: []
                for key in ("permanent_free", "event_free", "repeat_raid", "failed_attempts")
            }
            issues: list[Issue] = []
            for row in self.rows(
                "userQuest", ("questId", "clearNum", "challengeNum", "questPhase", "updatedAt")
            ):
                qid = _int(row, "questId")
                quest = self.master.quests.get(qid)
                if quest is None:
                    issues.append("MASTER_DATA_MISSING")
                    continue
                clear, phase, challenge = (
                    _int(row, "clearNum"),
                    _int(row, "questPhase"),
                    _int(row, "challengeNum"),
                )
                ignore = quest.ignore_failed or (clear > 0 and phase == 0)
                success = challenge if ignore else (clear + phase - 1 if clear > 0 else phase)
                failed = 0 if ignore else challenge - success
                if quest.uncertain_raid_history and not ignore:
                    # 保留通用公式的推算值，显式标记未应用账户特定历史补丁。
                    issues.append("UNCERTAIN_HISTORICAL_RULE")
                if success < 0 or failed < 0:
                    issues.append("PARTIAL_SOURCE")
                    continue
                updated = _int(row, "updatedAt")
                if quest.group in buckets:
                    buckets[quest.group].append((success, updated, qid, quest.name))
                if failed > 0:
                    buckets["failed_attempts"].append((failed, updated, qid, quest.name))
            result = {
                name: tuple(
                    QuestEntry(quest_id=qid, count=count, name=title)
                    for count, _, qid, title in sorted(rows, key=lambda r: (-r[0], -r[1], -r[2]))[
                        :100
                    ]
                )
                for name, rows in buckets.items()
            }
            return self.metric(
                QuestRankings(
                    permanent_free=result["permanent_free"],
                    event_free=result["event_free"],
                    repeat_raid=result["repeat_raid"],
                    failed_attempts=result["failed_attempts"],
                ),
                sources,
                "quests",
                tuple(issues),
            )
        except _Missing:
            return self.metric(None, sources, "quests")

    def class_score(self, servant_id: int, *, grand: bool = False) -> Metric[ClassScoreProgress]:
        sources: tuple[TableName, ...] = ("userClassBoardSquare",)
        rule = "grand_score" if grand else "class_score"
        svt = self.master.servants.get(servant_id) if self.master else None
        if self.master is None or svt is None or svt.class_id is None:
            return self.metric(None, sources, rule, needs_master=True)
        class_id = self.master.grand_class_ids.get(svt.class_id) if grand else svt.class_id
        boards = [
            b
            for b in self.master.class_boards.values()
            if b.grand == grand and class_id in b.class_ids
        ]
        if len(boards) != 1:
            return self.metric(None, sources, rule, needs_master=True)
        board = boards[0]
        try:
            rows = self.rows(
                "userClassBoardSquare",
                ("classBoardBaseId", "classBoardSquareIds", "classBoardUnlockSquareIds"),
            )
            matches = [r for r in rows if r["classBoardBaseId"] == board.id]
            if len(matches) != 1:
                raise _Missing
            enhanced = set(cast(tuple[int, ...], matches[0]["classBoardSquareIds"]))
            unlocked = set(cast(tuple[int, ...], matches[0]["classBoardUnlockSquareIds"]))
            issues: tuple[Issue, ...] = ()
            if (enhanced | unlocked) - board.all_square_ids:
                issues = ("MASTER_DATA_MISSING",)
            return self.metric(
                ClassScoreProgress(
                    board_id=board.id,
                    board_name=board.name,
                    enhanced_nodes=len(enhanced & board.skill_square_ids),
                    total_nodes=len(board.skill_square_ids),
                    unlocked_locks=len(unlocked & board.lock_square_ids),
                    total_locks=len(board.lock_square_ids),
                ),
                sources,
                rule,
                issues,
            )
        except _Missing:
            return self.metric(None, sources, rule)

    def support_ce(
        self, row: dict[str, Value], prefix: str, issues: list[Issue]
    ) -> SupportCraftEssence | None:
        ce_id = _int(row, prefix + "SvtId")
        if ce_id == 0:
            return None
        ce_static = self.master.craft_essences.get(ce_id) if self.master else None
        if ce_static is None:
            issues.append("MASTER_DATA_MISSING")
        return SupportCraftEssence(
            craft_essence_id=ce_id,
            name=ce_static.name if ce_static else None,
            level=_int(row, prefix + "Lv"),
            limit_break_count=_int(row, prefix + "LimitCount"),
            hp=cast(int | None, row.get(prefix + "Hp")),
            atk=cast(int | None, row.get(prefix + "Atk")),
        )

    def support_grand_flag(self, row: dict[str, Value], issues: list[Issue]) -> bool | None:
        reference = row["userSvtId"]
        flags = set(self.crown_flags.get(reference, ()))
        if "grandSvt" in row:
            flags.add(row["grandSvt"])
        if flags - {0, 1} or len(flags) > 1:
            issues.append("PARTIAL_SOURCE")
            return None
        if flags:
            return next(iter(flags)) == 1
        if "userSvtGrand" in self.snapshot.tables:
            return reference in self.crown_members
        return None

    def grand_supports(self) -> Metric[tuple[GrandSupportEntry, ...]]:
        sources: tuple[TableName, ...] = ("userSvtGrand", "userClassBoardSquare")
        try:
            rows = self.rows("userSvtGrand", ("grandGraphId", "userSvtId"))
            entries: list[GrandSupportEntry] = []
            issues: list[Issue] = []
            for row in sorted(rows, key=lambda r: _int(r, "grandGraphId")):
                equipment: list[GrandCraftEssenceSlot] = []
                for position, prefix in ((1, "ce"), (2, "ce2"), (3, "ce3")):
                    try:
                        ce = self.support_ce(row, prefix, issues)
                        effect_mode: Literal["original", "starting_np_50"] | None = None
                        if position == 2 and ce is not None:
                            mode = row.get("equipTarget2SkillChange")
                            if type(mode) is int and mode in (0, 1):
                                effect_mode = "original" if mode == 0 else "starting_np_50"
                                # 客户端字段读取已核；整数到效果仍借助Chaldea跨来源推断。
                                issues.append("PARTIAL_SOURCE")
                            else:
                                issues.append(
                                    "CAPTURE_FIELD_MISSING" if mode is None else "PARTIAL_SOURCE"
                                )
                        equipment.append(
                            GrandCraftEssenceSlot(
                                position=cast(Literal[1, 2, 3], position),
                                state="ready" if ce else "empty",
                                craft_essence=ce,
                                effect_mode=effect_mode,
                            )
                        )
                    except _Missing:
                        equipment.append(
                            GrandCraftEssenceSlot(
                                position=cast(Literal[1, 2, 3], position), state="unavailable"
                            )
                        )
                        issues.append("CAPTURE_FIELD_MISSING")
                try:
                    sid = _int(row, "svtId")
                    if sid == 0:
                        raise _Missing
                    svt = self.master.servants.get(sid) if self.master else None
                    if svt is None:
                        issues.append("MASTER_DATA_MISSING")
                    position_name: LiteralPosition = "all"
                    if svt:
                        position_name = cast(
                            LiteralPosition,
                            "extra"
                            if svt.class_group in {"extra1", "extra2"}
                            else svt.class_group
                            if svt.class_group != "unknown"
                            else "all",
                        )
                    grand_flag = self.support_grand_flag(row, issues)
                    if "appendSkillIds" not in row or "appendSkillLvs" not in row:
                        issues.append("CAPTURE_FIELD_MISSING")
                    servant = SupportSlot(
                        position=position_name,
                        state="ready",
                        servant_id=sid,
                        rarity=svt.rarity if svt else None,
                        name=svt.name if svt else None,
                        level=_int(row, "lv"),
                        np_level=_int(row, "treasureDeviceLv"),
                        skill_levels=(
                            _int(row, "skillLv1"),
                            _int(row, "skillLv2"),
                            _int(row, "skillLv3"),
                        ),
                        append_skill_ids=cast(tuple[int, ...] | None, row.get("appendSkillIds")),
                        append_skill_levels=cast(tuple[int, ...] | None, row.get("appendSkillLvs")),
                        grand=grand_flag,
                        append_skills=append_skills(row, "userSvtGrand", self.master),
                        active_skill_icons=active_skill_icons(row, "userSvtGrand", self.master),
                        class_id=svt.class_id if svt else None,
                        grand_class_id=self.master.grand_class_ids.get(svt.class_id)
                        if grand_flag and self.master and svt and svt.class_id
                        else None,
                        class_icon_key=class_icon_key(svt, self.master),
                        frame_type_base=frame_type_base(row, svt, self.master),
                        grand_class_icon_key=class_icon_key(svt, self.master, grand=True)
                        if grand_flag
                        else None,
                        parameters=support_parameters(
                            row,
                            context="grand_configuration",
                            grand=grand_flag,
                            grand_class_verified=bool(
                                self.master and svt and svt.class_id in self.master.grand_class_ids
                            ),
                        ),
                        craft_essence=equipment[0].craft_essence,
                        craft_essence_state=equipment[0].state,
                        class_score=self.class_score(sid),
                        grand_score=self.class_score(sid, grand=True) if grand_flag else None,
                    )
                except _Missing:
                    servant = SupportSlot(position="all", state="unavailable")
                    issues.append("CAPTURE_FIELD_MISSING")
                entries.append(
                    GrandSupportEntry(
                        graph_id=_int(row, "grandGraphId"),
                        servant=servant,
                        craft_essences=tuple(equipment),
                    )
                )
            return self.metric(tuple(entries), sources, "grand_supports", tuple(issues))
        except _Missing:
            return self.metric(None, sources, "grand_supports")

    def supports(self) -> Metric[tuple[SupportGroup, ...]]:
        sources: tuple[TableName, ...] = (
            "userGame",
            "userSvtLeader",
            "userSvtGrand",
            "userSupportDeck",
        )
        positions: tuple[LiteralPosition, ...] = (
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
        try:
            game = self.rows("userGame")[0]
            rows = self.rows("userSvtLeader", ("supportDeckId", "classId", "userSvtId", "svtId"))
            by_slot = {(_int(r, "supportDeckId"), _int(r, "classId")): r for r in rows}
            names = {
                _int(row, "supportDeckId"): row.get("name")
                for row in self.snapshot.tables.get("userSupportDeck", ())
            }

            issues: list[Issue] = []
            groups: list[SupportGroup] = []
            for kind, field, legacy in (
                ("normal", "fixMainSupportDeckIds", "mainSupportDeckId"),
                ("event", "fixEventSupportDeckIds", "eventSupportDeckId"),
            ):
                # 某一类缺失时仍保留另一类编成。回退必须由旧版选中字段明确指定，
                # 不能根据编队名称或存放的队长记录猜测当前编成。
                if field not in game:
                    issues.append("CAPTURE_FIELD_MISSING")
                    selected = game.get(legacy)
                    ids: tuple[int, ...] = (
                        (selected,) if type(selected) is int and selected > 0 else ()
                    )
                else:
                    ids = cast(tuple[int, ...], game[field])
                    if (
                        not isinstance(ids, tuple)
                        or len(ids) > 10
                        or any(type(i) is not int or i < 0 for i in ids)
                        or len(set(ids)) != len(ids)
                    ):
                        issues.append("CAPTURE_FIELD_MISSING")
                        continue
                for index, deck_id in enumerate((i for i in ids if i > 0), 1):
                    slots: list[SupportSlot] = []
                    for class_id, position in enumerate(positions):
                        row = by_slot.get((deck_id, class_id))
                        if row is None:
                            slots.append(SupportSlot(position=position, state="unavailable"))
                            issues.append("PARTIAL_SOURCE")
                            continue
                        if row["userSvtId"] == 0 and row["svtId"] == 0:
                            slots.append(SupportSlot(position=position, state="empty"))
                            continue
                        try:
                            sid = _int(row, "svtId")
                            if sid == 0 or row["userSvtId"] == 0:
                                raise _Missing
                            svt = self.master.servants.get(sid) if self.master else None
                            if svt is None:
                                issues.append("MASTER_DATA_MISSING")
                            ce = None
                            if "ceSvtId" not in row:
                                issues.append("CAPTURE_FIELD_MISSING")
                            elif _int(row, "ceSvtId") > 0:
                                ce_id = _int(row, "ceSvtId")
                                ce_static = (
                                    self.master.craft_essences.get(ce_id) if self.master else None
                                )
                                if ce_static is None:
                                    issues.append("MASTER_DATA_MISSING")
                                ce = SupportCraftEssence(
                                    craft_essence_id=ce_id,
                                    name=ce_static.name if ce_static else None,
                                    level=_int(row, "ceLv"),
                                    limit_break_count=_int(row, "ceLimitCount"),
                                    hp=cast(int | None, row.get("ceHp")),
                                    atk=cast(int | None, row.get("ceAtk")),
                                )
                            append_ids = cast(tuple[int, ...] | None, row.get("appendSkillIds"))
                            append_lvs = cast(tuple[int, ...] | None, row.get("appendSkillLvs"))
                            if "appendSkillIds" not in row or "appendSkillLvs" not in row:
                                issues.append("CAPTURE_FIELD_MISSING")
                            grand = self.support_grand_flag(row, issues)
                            slots.append(
                                SupportSlot(
                                    position=position,
                                    state="ready",
                                    servant_id=sid,
                                    rarity=svt.rarity if svt else None,
                                    name=svt.name if svt else None,
                                    level=_int(row, "lv"),
                                    np_level=_int(row, "treasureDeviceLv"),
                                    skill_levels=(
                                        _int(row, "skillLv1"),
                                        _int(row, "skillLv2"),
                                        _int(row, "skillLv3"),
                                    ),
                                    append_skill_ids=append_ids,
                                    append_skill_levels=append_lvs,
                                    craft_essence=ce,
                                    craft_essence_state="ready"
                                    if ce
                                    else "empty"
                                    if "ceSvtId" in row
                                    else "unavailable",
                                    grand=grand,
                                    append_skills=append_skills(row, "userSvtLeader", self.master),
                                    active_skill_icons=active_skill_icons(
                                        row, "userSvtLeader", self.master
                                    ),
                                    class_id=svt.class_id if svt else None,
                                    grand_class_id=self.master.grand_class_ids.get(svt.class_id)
                                    if grand and self.master and svt and svt.class_id
                                    else None,
                                    class_icon_key=class_icon_key(svt, self.master),
                                    frame_type_base=frame_type_base(row, svt, self.master),
                                    grand_class_icon_key=class_icon_key(
                                        svt, self.master, grand=True
                                    )
                                    if grand
                                    else None,
                                    parameters=support_parameters(
                                        row,
                                        context="normal_support",
                                        grand=grand,
                                        grand_class_verified=bool(
                                            self.master
                                            and svt
                                            and svt.class_id in self.master.grand_class_ids
                                        ),
                                    ),
                                    class_score=self.class_score(sid),
                                    grand_score=self.class_score(sid, grand=True)
                                    if grand
                                    else None,
                                )
                            )
                        except _Missing:
                            slots.append(SupportSlot(position=position, state="unavailable"))
                            issues.append("CAPTURE_FIELD_MISSING")
                    selected = game.get(legacy)
                    name = names.get(deck_id)
                    if name is None:
                        issues.append("CAPTURE_FIELD_MISSING")
                    groups.append(
                        SupportGroup(
                            kind=cast(Literal["normal", "event"], kind),
                            index=index,
                            name=cast(str | None, name),
                            legacy_selected=(selected == deck_id)
                            if isinstance(selected, int) and selected > 0
                            else None,
                            slots=tuple(slots),
                        )
                    )
            return self.metric(
                tuple(groups) if groups or not issues else None,
                sources,
                "supports",
                tuple(issues),
            )
        except (IndexError, _Missing):
            return self.metric(None, sources, "supports")

    def resources(self) -> tuple[Metric[ResourceBreakdown], ...]:
        output: list[Metric[ResourceBreakdown]] = []
        for kind, item_id, sources in (
            ("grail", 7999, (*_CARD_TABLES, "userItem")),
            ("crystal", 6999, (*_CARD_TABLES, "userSvtAppendPassiveSkillLv", "userItem")),
            ("lantern", 1000, ("userSvtCollection", "userItem")),
            ("great_lantern", 1001, ("userSvtCollection", "userItem")),
            ("unregistered_spirit", 17, ("userShop", "userItem")),
        ):
            source_tables = sources
            issues: list[Issue] = []
            consumed: int | None = None
            active: int | None = None
            append: int | None = None
            exchanges: int | None = None
            inventory: int | None = None
            try:
                inventory = next(
                    (
                        _int(r, "num")
                        for r in self.rows("userItem", ("itemId", "num"))
                        if _int(r, "itemId") == item_id
                    ),
                    0,
                )
            except _Missing:
                issues.append("CAPTURE_FIELD_MISSING")
            try:
                if kind == "grail":
                    consumed = sum(
                        max(0, _int(r, "exceedCount") - (2 if _int(r, "svtId") == 800100 else 0))
                        for t in _CARD_TABLES
                        for r in self.rows(t)
                    )
                elif kind == "crystal":
                    active = sum(
                        _int(r, k) == 10
                        for t in _CARD_TABLES
                        for r in self.rows(t)
                        for k in ("skillLv1", "skillLv2", "skillLv3")
                    )
                    append_rows = self.rows(
                        "userSvtAppendPassiveSkillLv", ("appendPassiveSkillLvs",)
                    )
                    append = sum(
                        level == 10
                        for r in append_rows
                        for level in cast(tuple[int, ...], r["appendPassiveSkillLvs"])
                    )
                    consumed = active + append
                elif kind in {"lantern", "great_lantern"}:
                    if self.master is None:
                        issues.append("MASTER_DATA_MISSING")
                    else:
                        counts = [
                            max(0, _int(r, "friendshipExceedCount") - (5 if sid == 800100 else 0))
                            for sid, r in self.owned().items()
                        ]
                        consumed = sum(
                            min(5, n) if kind == "lantern" else max(0, n - 5) for n in counts
                        )
                    if kind == "great_lantern":
                        issues.append("UPSTREAM_DISPLAY_OFFSET_EXCLUDED")
                else:
                    if "INCREMENTAL_TABLE_USED:userShop" in self.snapshot.warnings:
                        issues.append("PARTIAL_SOURCE")
                    if self.master is None:
                        issues.append("MASTER_DATA_MISSING")
                    else:
                        shop_rows = self.rows("userShop", ("shopId", "num"))
                        exchanges = 0
                        for row in shop_rows:
                            sid = _int(row, "shopId")
                            shop_type = self.master.shop_types.get(sid)
                            if shop_type is None:
                                issues.append("MASTER_DATA_MISSING")
                            if shop_type == 7 or (shop_type is None and sid // 100000 == 40):
                                exchanges += _int(row, "num")
                        consumed = exchanges * 10
            except _Missing:
                issues.append("CAPTURE_FIELD_MISSING")
            if consumed is None and inventory is None:
                output.append(self.metric(None, source_tables, kind, tuple(issues)))
            else:
                output.append(
                    self.metric(
                        ResourceBreakdown(
                            resource=cast("LiteralResource", kind),
                            represented_consumed=consumed,
                            current_inventory=inventory,
                            represented_total=consumed + inventory
                            if consumed is not None and inventory is not None
                            else None,
                            active_skill_consumed=active,
                            append_skill_consumed=append,
                            exchange_count=exchanges,
                        ),
                        source_tables,
                        kind,
                        tuple(issues),
                    )
                )
        return tuple(output)


# 类型别名只描述输出枚举，不从输入字符串扩展。
type LiteralPosition = Literal[
    "all", "saber", "archer", "lancer", "rider", "caster", "assassin", "berserker", "extra"
]


type LiteralResource = Literal[
    "lantern", "great_lantern", "grail", "crystal", "unregistered_spirit"
]


def build_report(
    snapshot: Snapshot,
    master: ReportMasterData | None,
    *,
    generated_at: int,
    selected_year: int | None = None,
    as_of_day: date | None = None,
    show_friend_code: bool = False,
    show_support: bool = False,
) -> AnnualReport:
    """不保存/发送结果；调用者只能在确认过的白名单快照上显式导出。"""
    if snapshot.region not in {"CN", "JP"}:
        raise CaptureError("REGION_UNSUPPORTED")
    if master is not None and master.region != snapshot.region:
        raise CaptureError("MASTER_REGION_MISMATCH")
    try:
        day = as_of_day or _date(snapshot.captured_at, snapshot.region)
        year = selected_year if selected_year is not None else day.year
        if selected_year is None and day.month == 1:
            games = snapshot.tables.get("userGame", ())
            try:
                if games and _date(_int(games[0], "createdAt"), snapshot.region).year < day.year:
                    year -= 1
            except _Missing:
                pass
        context = _Report(snapshot, master, year, day, show_friend_code)
        return AnnualReport(
            metadata=ReportMetadata(
                schema_version="jp_report.v1" if snapshot.region == "JP" else "cn_report.v1",
                region=snapshot.region,
                timezone="Asia/Tokyo" if snapshot.region == "JP" else "Asia/Shanghai",
                captured_at=snapshot.captured_at,
                generated_at=generated_at,
                as_of_day=day,
                selected_year=year,
                master_data_revision=master.revision if master else None,
            ),
            account=context.account(),
            favorites=context.favorites(),
            collection=context.collection(),
            summon_estimate=context.summon(),
            bond=context.bond(),
            gachas=context.gachas(),
            quests=context.quests(),
            resources=context.resources(),
            supports=context.supports() if show_support else None,
            grand_supports=context.grand_supports() if show_support else None,
        )
    except (_Missing, ValueError, OverflowError):
        raise CaptureError("REPORT_DATA_INVALID") from None

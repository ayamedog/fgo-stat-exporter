# ruff: noqa: E501
# HTML/CSS模板保留已验收布局。
"""白名单 cn_report.v1 → HTML 字符串；无抓包读取、网络访问或自动写盘。

build(report, demo=False, asset_dir=None) 接受 AnnualReport.model_dump(mode='json')。
所有素材按公开实体 ID 查本地文件。真实调用须显式取得显示好友码授权，
再由上层报告计算器填 account.friend_code；本模块不接触 userId。
"""

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import base_blocks as base

ROOT = Path(__file__).resolve().parent

num, e = base.num, base.e
CLASS_IDS = {
    "saber": 1,
    "archer": 2,
    "lancer": 3,
    "rider": 4,
    "caster": 5,
    "assassin": 6,
    "berserker": 7,
    "extra1": 1004,
    "extra2": 1005,
    "all": 1001,
    "extra": 1002,
}
LABELS = {
    "all": "ALL",
    "saber": "SABER",
    "archer": "ARCHER",
    "lancer": "LANCER",
    "rider": "RIDER",
    "caster": "CASTER",
    "assassin": "ASSASSIN",
    "berserker": "BERSERKER",
    "extra": "EXTRA",
}


def value(r: dict[str, Any], key: str) -> Any:
    m = r.get(key)
    return m.get("value") if m and m.get("state") in ("ready", "partial") else None


def image_tag(filename: str, asset_dir: Path, cls: str = "", alt: str = "") -> str:
    if not (asset_dir / filename).is_file():
        return f'<div class="{e(cls)} image-missing">未提供<br>图片</div>'
    return f'<img class="{e(cls)}" src="assets/{e(filename)}" alt="{e(alt)}">'


def profile(r: dict[str, Any], demo: bool) -> str:
    result = base.profile(r)
    a = value(r, "account")
    if a is None:
        return result
    streak = f'<div class="sep"></div><div><div class="label">连续登录</div><div class="number value">{num(a.get("consecutive_login_days"))}<span class="unit">天</span></div></div>'
    end = result.rfind("</div></div>")
    result = result[:end] + streak + result[end:]
    friend = a.get("friend_code")
    if not friend:
        return result
    if not re.fullmatch(r"(?:[0-9]{9}|[0-9]{12,20})", friend):
        raise ValueError("REPORT_FRIEND_CODE_FORMAT")
    formatted = ",".join(friend[i : i + 3] for i in range(0, len(friend), 3))
    mark = "<span>演示</span>" if demo else ""
    size = ' style="font-size:17px;letter-spacing:0"' if len(friend) > 12 else ""
    return result.replace(
        "<p>账号已隐藏 · 个人统计快照</p>",
        f'<p class="friend-caption">好友 ID {mark}</p><div class="friend-id number"{size}>{formatted}</div>',
    )


def hero(r: dict[str, Any], asset_dir: Path) -> str:
    c, b, f = value(r, "collection"), value(r, "bond"), value(r, "favorites")
    c, b, f = c or {}, b or {}, f or []
    fav = ""
    for s in f[:2]:
        name = s.get("name") or f"从者 #{s['servant_id']}"
        role = "单推" if "push" in s["roles"] else "中意"
        face = image_tag(f"status-{s['servant_id']}.png", asset_dir, "portrait", name)
        fav += f"""<div class="favorite">{face}<div><div class="role">{role}从者</div><h3>{e(name)}</h3><div class="meta">Lv. <b>{num(s["level"])}</b> · 宝具 <b>{num(s["np_level"])}</b></div><div class="meta">羁绊 <b>{num(s["bond_level"])}</b> / {num(s["bond_cap"])}</div></div></div>"""
    if not fav:
        fav = base.MISSING
    selected = next((s for s in f if "push" in s["roles"]), f[0] if f else None)
    if selected:
        sid = selected["servant_id"]
        name = selected.get("name") or f"从者 #{sid}"
        art = f"hero-{sid}.png"
        if not (asset_dir / art).is_file():
            art = (
                "castoria-art.png"
                if sid == 504500 and (asset_dir / "castoria-art.png").is_file()
                else f"status-{sid}.png"
            )
        art_class = "hero-status" if art.startswith("status-") else ""
        portrait = image_tag(art, asset_dir, art_class, name + "原生游戏素材")
    else:
        name = "单推从者未提供"
        portrait = '<div class="hero-empty">✦<br>待写入的回忆</div>'
    return f"""<div class="hero"><div class="hero-copy"><div class="tagline"><span>MEMORY PIECE / 01</span> THE JOURNEY CONTINUES</div><h2>相遇，都留有回响。</h2><div class="highlights"><div class="highlight"><span class="label">已契约英灵</span><b class="number">{num(c.get("owned_types"))}</b><span class="suffix">位，汇聚于此</span></div><div class="highlight"><span class="label">羁绊的见证</span><b class="number">{num(b.get("currently_held_bond_ces"))}</b><span class="suffix">张礼装，珍藏至今</span></div><div class="highlight"><span class="label">羁绊达到 15</span><b class="number">{num(b.get("bond_15_servants"))}</b><span class="suffix">位，无可替代</span></div></div><div class="favorite-pills">{fav}</div></div><figure class="polaroid"><div class="photo-back"></div><span class="photo-tab">MY CHALDEA</span>{portrait}<figcaption class="photo-caption"><b>{e(name)}</b><span>MY FAVORITE</span></figcaption><span class="tiny-star">✦</span><img class="photo-quartz" src="assets/item-2.png" alt="圣晶石装饰"></figure></div>"""


def rankings(r: dict[str, Any], asset_dir: Path, demo: bool) -> str:
    g = value(r, "gachas")
    if g is None:
        return base.MISSING
    panels = []
    japanese = r["metadata"].get("region") == "JP"
    prefix = "jp-" if japanese else ""
    timezone = ZoneInfo(r["metadata"].get("timezone", "Asia/Shanghai"))
    for key, title in [
        ("selected_pool_year", f"{r['metadata']['selected_year']} 年卡池"),
        ("all_time", "历年卡池"),
    ]:
        rows = ""
        for i, a in enumerate(g[key][:3]):
            fmt = "%Y.%m.%d" if key == "all_time" else "%m.%d"
            dates = " – ".join(
                datetime.fromtimestamp(a[k], timezone).strftime(fmt)
                if a.get(k)
                else "未提供"
                for k in ["opened_at", "closed_at"]
            )
            candidates = [
                f"banner-{prefix}image-{a.get('banner_image_id')}.png",
                f"banner-{a['gacha_id']}.png",
            ]
            banner_file = next(
                (x for x in candidates if (asset_dir / x).is_file()),
                "banner-jp-unavailable.png" if japanese else "banner-permanent-fallback.png",
            )
            fallback = banner_file == "banner-permanent-fallback.png"
            fallback_label = (
                '<div class="fallback-label">缺图 · 常驻池示意</div>' if fallback else ""
            )
            name = a.get("name") or f"未知卡池 #{a['gacha_id']}"
            short = (
                name.replace("【每周替换】", "每周替换 · ")
                .replace("（魔术师）", "")
                .replace("「冠位戴冠战：Extra」", "冠位戴冠战 · Extra")
            )
            banner = image_tag(
                banner_file,
                asset_dir,
                "",
                ("常驻池兜底示意" if fallback else name + ("日服" if japanese else "国服") + "卡池横幅"),
            )
            rows += f"""<div class="banner-rank"><div class="banner-frame"><span class="rank-sticker">0{i + 1}</span>{banner}</div><div class="banner-meta"><h4>{e(short)}</h4>{fallback_label}<p>{dates}</p><div class="banner-count number">{num(a["cumulative_pulls"])}<small>抽</small></div></div></div>"""
        if not rows:
            rows = '<div class="missing">无可显示记录</div>'
        panels.append(
            f'<div class="panel ranking-panel"><div class="ranking-head"><h3>{title}</h3><span>SUMMONING / TOP 3</span></div>{rows}</div>'
        )
    note = "真实国服卡池图 · 抽数为演示。" if demo else ""
    return (
        '<div class="rankings">'
        + "".join(panels)
        + f'</div><p class="rankings-note">{note}“年度卡池”按开闭年份筛选，数值为池累计抽数。</p>'
    )


def collection(r: dict[str, Any]) -> str:
    c = value(r, "collection")
    if c is None:
        return base.MISSING
    s = base.collection(r)
    data = [(k, v) for k, v in c["by_class"].items() if v > 0]
    total = sum(v for _, v in data)
    circ = 2 * math.pi * 115
    off = 0
    arcs = ""
    labels = ""
    symbols = ""
    for i, (key, count) in enumerate(data):
        fraction = count / total
        length = fraction * circ
        color = base.COLORS[i % len(base.COLORS)]
        arcs += f'<circle cx="165" cy="165" r="115" fill="none" stroke="{color}" stroke-width="48" stroke-dasharray="{max(0, length - 3):.3f} {circ - max(0, length - 3):.3f}" stroke-dashoffset="{-off:.3f}"/>'
        angle = (off + length / 2) / circ * 2 * math.pi - math.pi / 2
        x = 165 + math.cos(angle) * 113
        y = 165 + math.sin(angle) * 113
        labels += f'<text x="{x:.2f}" y="{y + 5:.2f}" text-anchor="middle" font-family="Liberation Sans" font-size="16" font-weight="bold" fill="#153f59">{count}</text>'
        ix = 165 + math.cos(angle) * 145 - 17
        iy = 165 + math.sin(angle) * 145 - 17
        cid = CLASS_IDS.get(key)
        if cid is not None:
            symbols += f'<img class="ring-symbol" style="left:{ix:.2f}px;top:{iy:.2f}px" src="assets/class-{cid}.png" alt="{base.CLASS_NAMES[key]}职阶 {count} 位">'
        off += length
    chart = f'<div class="donut"><svg width="330" height="330" viewBox="0 0 330 330" aria-label="职阶分布，图标对应环形分区"><g transform="rotate(-90 165 165)">{arcs}</g>{labels}</svg><div class="donut-center"><span class="label">契约从者</span><span class="number">{num(c["owned_types"])}</span><span class="total">/ {num(c["released_types"])} 位</span></div>{symbols}</div>'
    # The original legend is replaced, not duplicated below the ring.
    return re.sub(
        r'<div class="donut">.*?<div class="collection-right">',
        chart + '</div><div class="collection-right">',
        s,
        flags=re.S,
    )


def bond(r: dict[str, Any]) -> str:
    b = value(r, "bond")
    if b is None:
        return base.MISSING
    # 年份按可用数据分配宽度，不把八年柱宽固定用于长龄账号。
    years = b["by_acquired_year"]
    maximum = max((s["count"] for s in years), default=0) or 1
    step = 620 / max(len(years), 1)
    width = min(32, step * 0.48)
    svg = '<svg viewBox="0 0 620 232" role="img" aria-label="当前持有羁绊礼装按获得年统计"><line x1="0" y1="196" x2="620" y2="196" stroke="#c9e1ed"/>'
    for i, s in enumerate(years):
        x = (i + 0.5) * step
        h = s["count"] / maximum * 145
        color = "#388bb4" if s["year"] == r["metadata"]["selected_year"] else "#acd4e4"
        svg += f'<rect x="{x - width / 2:.2f}" y="{196 - h:.2f}" width="{width:.2f}" height="{h:.2f}" rx="5" fill="{color}"/><text x="{x:.2f}" y="{186 - h:.2f}" font-family="Liberation Sans" font-size="20" font-weight="bold" text-anchor="middle" fill="#54849e">{s["count"]}</text><text x="{x:.2f}" y="222" font-family="Liberation Sans" font-size="15" text-anchor="middle" fill="#7c98a9">{str(s["year"])[-2:]}</text>'
    svg += "</svg>"
    # Avoid a zero denominator in a valid account with no eligible CE types.
    if not b["eligible_bond_ce_types"]:
        return '<div class="missing">当前没有可计入的羁绊礼装种类</div>'
    if not years:
        clone = {
            **r,
            "bond": {
                **r["bond"],
                "value": {
                    **b,
                    "by_acquired_year": [{"year": r["metadata"]["selected_year"], "count": 0}],
                },
            },
        }
        result = base.bond(clone)
    else:
        result = base.bond(r)
    return re.sub(r'<svg viewBox="0 0 620 232".*?</svg>', svg, result, flags=re.S)


def resources(r: dict[str, Any]) -> str:
    s = base.resources(r)
    for name, iid in zip(
        ["lantern", "grail", "crystal", "unregistered_spirit"], [1000, 7999, 6999, 17], strict=True
    ):
        s = s.replace(
            base.icon(name), f'<img class="item-icon" src="assets/item-{iid}.png" alt="{name}">'
        )
    great = next(
        (
            m["value"]
            for m in r["resources"]
            if m.get("value") and m["value"]["resource"] == "great_lantern"
        ),
        None,
    )
    label = (
        "大梦火：未提供" if great is None else f"大梦火：库存 {num(great.get('current_inventory'))}"
    )
    if great is None:
        s = s.replace(
            '<span class="unknown-pill">大梦火：未提供</span>',
            f'<span class="unknown-pill"><img src="assets/item-1001.png" alt="大梦火">{label}</span>',
        )
    else:
        row = f'<div class="resource-row"><div class="resource-icon"><img class="item-icon" src="assets/item-1001.png" alt="大梦火"></div><div class="resource-name">大梦火</div><div class="resource-formula"><span class="v">{num(great.get("represented_consumed"))}</span><span class="label">已用</span><span class="plus">+</span><span class="v">{num(great.get("current_inventory"))}</span><span class="label">库存</span><span class="plus">=</span><span class="sum">{num(great.get("represented_total"))}</span></div></div>'
        s = s.replace('<div class="resource-note">', row + '<div class="resource-note">').replace(
            '<span class="unknown-pill">大梦火：未提供</span>', ""
        )
    return s


def score_line(metric: dict[str, Any] | None, label: str = "职阶星图") -> str:
    if not metric or metric.get("state") not in ("ready", "partial") or not metric.get("value"):
        return f'<div class="score-line unknown"><span>{label}</span><b>未提供</b></div>'
    v = metric["value"]
    done = v["enhanced_nodes"]
    total = v["total_nodes"]
    pct = 100 * done / total if total else 0
    marker = "*" if metric.get("state") == "partial" else ""
    locks = f"锁 {v['unlocked_locks']}/{v['total_locks']}"
    return f'<div class="score-line"><span>{label}</span><b>{done}/{total}{marker}</b><em>{locks}</em><div class="score-track"><i style="width:{pct:.3f}%"></i></div></div>'


def grand_supports(r: dict[str, Any], asset_dir: Path, demo: bool) -> str:
    data = value(r, "grand_supports")
    if data is None:
        return '<div class="missing">冠位编成 · 数据未提供</div>'
    if not data:
        return '<div class="missing">暂无冠位编成记录</div>'
    cards = ""
    for entry in data:
        a = entry["servant"]
        cid = CLASS_IDS.get(a["position"], 1002)
        name = a.get("name") or (
            f"从者 #{a.get('servant_id')}" if a.get("servant_id") else "从者数据未提供"
        )
        face = image_tag(f"status-{a.get('servant_id')}.png", asset_dir, "grand-face", name)
        skills = " / ".join(str(v) for v in (a.get("skill_levels") or [])) or "未提供"
        ces = ""
        for slot in entry["craft_essences"]:
            ce = slot.get("craft_essence")
            pos = slot["position"]
            if slot["state"] == "ready" and ce:
                cn = ce.get("name") or f"礼装 #{ce['craft_essence_id']}"
                im = image_tag(f"ce-{ce['craft_essence_id']}.png", asset_dir, "grand-ce-image", cn)
                lb = "满破" if ce["limit_break_count"] >= 4 else f"突破{ce['limit_break_count']}"
                ces += f'<div class="grand-ce-slot"><span class="ce-position">礼装 {pos}</span>{im}<b>{e(cn)}</b><span class="ce-level">Lv.{num(ce["level"])} · {lb}</span></div>'
            else:
                label = "空位" if slot["state"] == "empty" else "未提供"
                ces += f'<div class="grand-ce-slot"><span class="ce-position">礼装 {pos}</span><div class="ce-empty">◇</div><b>{label}</b></div>'
        scores = score_line(a.get("class_score")) + score_line(a.get("grand_score"), "冠位星图")
        cards += f'<div class="grand-card"><div class="grand-card-head"><img src="assets/class-{cid}.png" alt="职阶"><b>{LABELS[a["position"]]}</b><em>冠位编成</em></div>{face}<div class="grand-info"><h4>{e(name)}</h4><p>Lv.<b>{num(a.get("level"))}</b> · 宝具 {num(a.get("np_level"))}</p><span>技能 {skills}</span></div><div class="grand-scores">{scores}</div><div class="grand-ces">{ces}</div></div>'
    mark = "演示配置 · " if demo else ""
    partial = (
        '<div class="partial-badge">冠位编成 · 部分数据</div>'
        if r["grand_supports"]["state"] == "partial"
        else ""
    )
    return (
        partial
        + f'<div class="grand-group"><div class="grand-group-head"><h3>冠位编成 <span>专用三礼装配置</span></h3><b>GRAND FORMATION</b></div><div class="grand-grid">{cards}</div></div><p class="support-note">{mark}是否作为可借助战，需符合游戏当前开放与摆放规则；冠位星图不代表普通关卡生效。</p>'
    )


def supports(r: dict[str, Any], asset_dir: Path, demo: bool) -> str:
    data = value(r, "supports")
    if data is None:
        return base.MISSING + grand_supports(r, asset_dir, demo)
    if not data:
        return '<div class="missing">无助战编组</div>' + grand_supports(r, asset_dir, demo)
    groups = []
    for group in sorted(data, key=lambda g: (g["kind"] == "event", g["index"])):
        mode = group["kind"]
        kind = "普通" if mode == "normal" else "活动"
        cards = ""
        slots = sorted(group["slots"], key=lambda s: list(LABELS).index(s["position"]))
        for i, a in enumerate(slots):
            cid = CLASS_IDS[a["position"]]
            head = f'<div class="support-slot"><img src="assets/class-{cid}.png" alt="职阶图标"><b>{LABELS[a["position"]]}</b><span>{i + 1:02}</span></div>'
            if a["state"] != "ready":
                label = "空位" if a["state"] == "empty" else "数据未提供"
                cards += f'<div class="support-card">{head}<div class="support-empty">◇<br>{label}</div></div>'
                continue
            grand = a.get("grand")
            grand_label = "戴冠" if grand is True else "未戴冠" if grand is False else "戴冠未提供"
            grand_cls = "grand-badge crowned" if grand is True else "grand-badge"
            head = head.replace("</div>", f'<em class="{grand_cls}">{grand_label}</em></div>')
            name = a.get("name") or f"从者 #{a['servant_id']}"
            skill = " / ".join(str(v) for v in a["skill_levels"])
            face = image_tag(f"status-{a['servant_id']}.png", asset_dir, "support-face", name)
            ce = a.get("craft_essence")
            if ce:
                ce_name = ce.get("name") or f"礼装 #{ce['craft_essence_id']}"
                ce_image = image_tag(
                    f"ce-{ce['craft_essence_id']}.png", asset_dir, "support-ce", ce_name
                )
                ce_break = (
                    "满破" if ce["limit_break_count"] >= 4 else f"突破 {ce['limit_break_count']}"
                )
                ce_html = f'{ce_image}<div class="support-ce-info"><b>{e(ce_name)}</b><span>Lv. {num(ce["level"])} · {ce_break}</span></div>'
            else:
                ce_html = f'<div class="support-ce-info"><b>{("未装备礼装" if a.get("craft_essence_state") == "empty" else "礼装未提供")}</b></div>'
            scores = score_line(a.get("class_score")) + (
                score_line(a.get("grand_score"), "冠位星图") if a.get("grand") is True else ""
            )
            cards += f"""<div class="support-card">{head}{face}<div class="support-info"><h4>{e(name)}</h4><p>Lv. <b>{num(a["level"])}</b><em>宝具 {num(a["np_level"])}</em></p><div class="skill-levels"><span>技能</span><b>{skill}</b></div></div>{ce_html}<div class="support-scores">{scores}</div></div>"""
        mark = "DEMO / " if demo else ""
        groups.append(
            f"""<div class="support-group {mode}"><div class="support-group-head"><h3><span>{kind}</span>助战 · 编组 {group["index"]:02}</h3><div>{mark}9 SLOTS</div></div><div class="support-grid">{cards}</div></div>"""
        )
    note = (
        "以上从者、礼装、宝具与技能均为演示配置。"
        if demo
        else "空位与数据未提供分开显示；不据旧字段推定唯一生效编组。"
    )
    return (
        "".join(groups)
        + f'<p class="support-note">普通与活动分开显示；星图：已强化节点 / 全部技能节点；锁单独统计。节点记录不代表效果开放条件全部满足，* 为部分数据。{note}</p>'
        + grand_supports(r, asset_dir, demo)
    )


def build(
    r: dict[str, Any],
    *,
    demo: bool = False,
    asset_dir: Path | None = None,
    support_html: str | None = None,
) -> str:
    """返回 HTML 字符串，不写盘。asset_dir 仅接受调用方核验的本地公开素材目录。"""
    asset_dir = Path(asset_dir or ROOT / "assets").resolve()
    t = (ROOT / "template.html").read_text(encoding="utf-8")
    show_support = r.get("supports") is not None or r.get("grand_supports") is not None
    support_block = ""
    if show_support:
        support_block = supports(r, asset_dir, demo) if support_html is None else support_html
    t = t.replace(
        "{{SUPPORT_SECTION}}",
        '<section class="section"><div class="section-head"><div class="index">05</div><h2>助战编成</h2><span class="en">SUPPORT LINEUP</span></div>{{SUPPORTS}}</section>'
        if show_support
        else "",
    )
    vals = {
        "PROFILE": profile(r, demo),
        "HERO": hero(r, asset_dir),
        "SUMMON": base.summon(r),
        "COLLECTION": collection(r),
        "BOND": bond(r),
        "RANKINGS": rankings(r, asset_dir, demo),
        "QUESTS": base.quests(r),
        "RESOURCES": resources(r),
        "SUPPORTS": support_block,
    }
    if any((r.get(k) or {}).get("state") == "partial" for k in ("collection", "bond", "favorites")):
        vals["HERO"] = (
            '<div class="partial-badge">顶部统计 · 部分数据，数量仅含可识别记录</div>'
            + vals["HERO"]
        )
        if (r.get("collection") or {}).get("state") == "partial":
            vals["HERO"] = vals["HERO"].replace("已契约英灵", "已映射契约英灵")
        if (r.get("bond") or {}).get("state") == "partial":
            vals["HERO"] = (
                vals["HERO"]
                .replace("羁绊的见证", "已识别羁绊礼装")
                .replace("羁绊达到 15", "已识别羁绊 15")
            )
    for key, block in vals.items():
        metric_key = {
            "PROFILE": "account",
            "SUMMON": "summon_estimate",
            "COLLECTION": "collection",
            "BOND": "bond",
            "RANKINGS": "gachas",
            "QUESTS": "quests",
            "SUPPORTS": "supports",
        }.get(key)
        metric = r.get(metric_key) if metric_key else None
        if metric and metric["state"] == "partial":
            block = '<div class="partial-badge">部分数据 · 按当前可追溯记录</div>' + block
        t = t.replace("{{" + key + "}}", block)
    timestamp = datetime.fromtimestamp(
        r["metadata"]["captured_at"], ZoneInfo(r["metadata"].get("timezone", "Asia/Shanghai"))
    ).strftime("%Y-%m-%d %H:%M")
    t = t.replace("{{SNAPSHOT}}", timestamp).replace(
        "<b>2026</b>", f"<b>{r['metadata']['selected_year']}</b>"
    )
    if r["metadata"].get("region") == "JP":
        t = t.replace("Asia/Shanghai", "Asia/Tokyo").replace("年份按国服时区划分", "年份按日服时区划分")
    if not demo:
        t = (
            t.replace("DEMO · 演示数据", "ACCOUNT · 统计快照")
            .replace(
                "设计预览 · 所有账号统计、好友 ID 与助战配置均为演示",
                "账号统计快照 · 口径与数据覆盖范围见各模块注释",
            )
            .replace("不包含真实账号数据", "好友 ID 依用户选择显示")
            .replace("FGO 御主纪念册 · C 风格演示", "FGO 御主纪念册")
        )
    css = """.image-missing{display:flex;align-items:center;justify-content:center;text-align:center;font-size:13px;color:#7d9ead;background:#ecf6fa;border:1px dashed #c3dce7}.polaroid img.hero-status{object-fit:contain;background:linear-gradient(145deg,#e1f5fa,#fff9df)}.hero-empty{height:382px;display:flex;align-items:center;justify-content:center;text-align:center;background:#eef8fc;color:#8eafbc;font-size:22px}.support-empty{position:absolute;left:0;right:0;top:64px;text-align:center;color:#94adb9;font-size:18px}.partial-badge{color:#927540;background:#fff5d4;font-size:13px;padding:4px 10px;margin-bottom:8px;display:inline-block}.support-info h4{white-space:normal;max-height:45px;overflow:hidden}.support-info p{margin-top:2px}.support-info h4{font-size:14px}.favorite h3{font-size:14px}.support-ce-info b{font-size:11px;max-height:35px;overflow:hidden}.banner-meta h4{max-height:68px;overflow:hidden}"""
    t = t.replace("</style>", css + "</style>")
    # Only trusted, fixed asset filenames are interpolated into the directory URI.
    return t.replace('src="assets/', f'src="{asset_dir.as_uri()}/')

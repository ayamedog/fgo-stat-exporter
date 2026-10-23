# ruff: noqa: E501
# HTML/CSS 模板长行保留已验收视觉；其他规则正常检查。
"""纯本地报告预览：已确认的白名单 cn_report.v1 JSON → HTML → 1200px PNG。

只渲染白名单指标，图片仅可取固定本地资源映射。无抓包读取、远程脚本或网络请求。
"""

import html
import math
from typing import Any

CLASS_NAMES = {
    "saber": "剑",
    "archer": "弓",
    "lancer": "枪",
    "rider": "骑",
    "caster": "术",
    "assassin": "杀",
    "berserker": "狂",
    "extra1": "EXⅠ",
    "extra2": "EXⅡ",
    "unknown": "未知",
}
COLORS = [
    "#378db8",
    "#4aa6c7",
    "#71c8cf",
    "#7bb3cf",
    "#4679a6",
    "#7e9dcc",
    "#b4a9d6",
    "#96c9c3",
    "#bedbae",
    "#bbc9d0",
]
MISSING = '<div class="missing">未提供 · 等待对应数据</div>'


def e(v: object) -> str:
    return html.escape(str(v), quote=True)


def num(v: int | None) -> str:
    return f"{v:,}" if v is not None else "—"


def val(report: dict[str, Any], key: str) -> Any:
    m = report[key]
    return m["value"] if m["state"] in ("ready", "partial") else None


def profile(r: dict[str, Any]) -> str:
    a = val(r, "account")
    if a is None:
        return MISSING
    master_name = a.get("master_name") or "昵称未提供"
    region_name = "日服" if r["metadata"].get("region") == "JP" else "国服"
    return f"""<div class="profile"><div class="avatar">M</div><div class="identity"><h2>{e(master_name)}<span class="region">{region_name}</span></h2><p>账号已隐藏 · 个人统计快照</p></div><div class="profile-stats"><div><div class="label">启程之日</div><div class="number date">{e(a["created_day"])}</div></div><div class="sep"></div><div><div class="label">成为御主</div><div class="number value">{num(a["tenure_days"])}<span class="unit">天</span></div></div><div class="sep"></div><div><div class="label">累计登录</div><div class="number value">{num(a["total_login_days"])}<span class="unit">天</span></div></div></div></div>"""


def summon(r: dict[str, Any]) -> str:
    s = val(r, "summon_estimate")
    if s is None:
        return MISSING
    rate = s["adjusted_ssr_acquisitions"] / s["adjusted_pulls"] * 100
    return f"""<div class="panel summon"><div class="summon-left"><div class="kicker">五星累计占比<span class="estimated">估算</span></div><div class="number rate">{rate:.2f}<span class="unit">%</span></div><div class="summon-math"><span class="number">{num(s["adjusted_ssr_acquisitions"])}</span><span class="text">次</span><span class="divider">/</span><span class="number">{num(s["adjusted_pulls"])}</span><span class="text">抽 · 含福袋</span></div></div><div class="summon-right"><div class="lucky"><div class="lucky-mark"><span>✦</span></div><div><span class="number">+ {num(s["lucky_bag_records"])}</span><span class="text">福袋记录</span></div></div><div class="note">按图鉴累计获取数与卡池累计抽数估算，<br>五星次数扣除福袋保底；总抽数含福袋，<br>不等同于逐抽中奖率。</div></div></div>"""


def collection(r: dict[str, Any]) -> str:
    c = val(r, "collection")
    if c is None:
        return MISSING
    data = [(k, v) for k, v in c["by_class"].items() if v > 0]
    total = sum(v for _, v in data)
    circ = 2 * math.pi * 106
    off = 0
    arcs = ""
    legend = ""
    for i, (k, v) in enumerate(data):
        length = v / total * circ
        arcs += f'<circle cx="139" cy="139" r="106" fill="none" stroke="{COLORS[i % len(COLORS)]}" stroke-width="34" stroke-dasharray="{max(0, length - 3):.3f} {circ - max(0, length - 3):.3f}" stroke-dashoffset="{-off:.3f}"/>'
        off += length
        legend += f'<div class="class-item"><i style="background:{COLORS[i % len(COLORS)]}"></i><span>{CLASS_NAMES[k]}</span><b>{v}</b></div>'
    rarities = ""
    for i, s in enumerate(sorted(c["by_rarity"], key=lambda row: row["rarity"], reverse=True)):
        fill = 100 * s["owned"] / s["released"] if s["released"] else 0
        rarities += f'<div class="rarity"><div class="label">{s["rarity"]}★</div><div class="track"><div class="fill" style="width:{fill:.3f}%;background:{["#438fb6", "#60a6c5", "#79b7ce", "#99cadb", "#b7dce5", "#c8e7ed"][min(i, 5)]}"></div></div><div class="amount">{s["owned"]} / {s["released"]}</div></div>'
    return f"""<div class="panel collection"><div><div class="donut"><svg width="278" height="278" viewBox="0 0 278 278" role="img" aria-label="已契约从者按职阶分布"><g transform="rotate(-90 139 139)">{arcs}</g></svg><div class="donut-center"><span class="label">契约从者</span><span class="number">{num(c["owned_types"])}</span><span class="total">/ {num(c["released_types"])} 位</span></div></div><div class="class-legend">{legend}</div></div><div class="collection-right"><div class="collection-stats"><div><div class="label">五星契约种类</div><div class="number">{num(c["owned_ssr_types"])}</div></div><div><div class="label">当前五星宝具总数</div><div class="number">{num(c["current_ssr_np_levels"])}</div></div></div>{rarities}<div class="collection-note">图鉴进度：已契约 / 区服已实装 · 宝具数按当前持有统计</div></div></div>"""


def bond(r: dict[str, Any]) -> str:
    b = val(r, "bond")
    year = r["metadata"]["selected_year"]
    if b is None:
        return MISSING
    progress = b["currently_held_bond_ces"] / b["eligible_bond_ce_types"] * 100
    years = b["by_acquired_year"]
    maximum = max(s["count"] for s in years) or 1
    bars = '<svg viewBox="0 0 620 232" role="img" aria-label="当前持有羁绊礼装按获得年统计"><line x1="0" y1="196" x2="620" y2="196" stroke="#c9e1ed"/>'
    for i, s in enumerate(years):
        x = 22 + i * 77
        h = s["count"] / maximum * 145
        y = 196 - h
        color = "#388bb4" if s["year"] == year else "#acd4e4"
        bars += f'<rect x="{x}" y="{y:.2f}" width="32" height="{h:.2f}" rx="5" fill="{color}"/><text x="{x + 16}" y="{y - 10:.2f}" font-family="Liberation Sans" font-size="20" font-weight="bold" text-anchor="middle" fill="#54849e">{s["count"]}</text><text x="{x + 16}" y="222" font-family="Liberation Sans" font-size="15" text-anchor="middle" fill="#7c98a9">{str(s["year"])[-2:]}</text>'
    bars += "</svg>"
    return f"""<div class="panel bond"><div class="bond-left"><div class="bond-big"><div class="label">当前持有羁绊礼装</div><div class="number">{num(b["currently_held_bond_ces"])}<small> / {num(b["eligible_bond_ce_types"])}</small></div></div><div class="bond-progress"><span>收集进度</span><strong>{progress:.1f}%</strong></div><div class="track"><div class="fill" style="width:{progress:.3f}%"></div></div><div class="bond-minis"><div class="bond-mini"><div class="number">{num(b["current_year_held_bond_ces"])}</div><div class="label">{year} 年获得</div></div><div class="bond-mini"><div class="number">{num(b["bond_15_servants"])}</div><div class="label">羁绊 15 从者</div></div></div></div><div class="bond-chart"><div class="chart-heading">按获得年份分布<span>单位：张</span></div>{bars}</div><p class="note">统计当前持有及仓库中的礼装；出售或缺失实例不会计入。年份按国服时区划分。</p></div>"""


def quests(r: dict[str, Any]) -> str:
    q = val(r, "quests")
    if q is None:
        return MISSING
    cards = ""
    for key, title, icon in [
        ("permanent_free", "最多常驻 Free", "◇"),
        ("event_free", "最多活动 Free", "✧"),
        ("repeat_raid", "最多重复击退战", "⌁"),
        ("failed_attempts", "最多失败次数", "☆"),
    ]:
        a = q[key][0] if q[key] else None
        cards += f"""<div class="quest-card"><div class="quest-icon">{icon}</div><div><div class="quest-label">{title}</div><div class="quest-name">{e(a["name"]) if a else "未提供"}</div></div><div class="number quest-count">{num(a["count"]) if a else "—"}<small>次</small></div></div>"""
    return (
        '<div class="quest-grid">'
        + cards
        + '</div><p class="quest-note">仅依据快照覆盖的关卡记录；高难历史例外与缺项需单独校验。</p>'
    )


def icon(name: str) -> str:
    paths = {
        "lantern": '<path d="M16 3v4m-5 2h10l-2 13h-6L11 9zm-2 16h14M13 7h6M16 12v6"/>',
        "grail": '<path d="M10 6h12v8a6 6 0 0 1-12 0V6zm0 2H6v5a5 5 0 0 0 5 5m11-10h4v5a5 5 0 0 1-5 5M16 20v6m-5 1h10"/>',
        "crystal": '<path d="m16 3 11 10-11 16L5 13 16 3zM5 13h22M16 3l-5 10 5 16 5-16-5-10z"/>',
        "unregistered_spirit": '<path d="m16 3 4 8 9 5-9 4-4 9-4-9-9-4 9-5 4-8zm0 8v10m-5-5h10"/>',
    }
    return (
        '<svg viewBox="0 0 32 32" fill="none" stroke="#4694b8" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round">'
        + paths[name]
        + "</svg>"
    )


def resources(r: dict[str, Any]) -> str:
    names = {
        "lantern": "迦勒底梦火",
        "grail": "圣杯",
        "crystal": "传承结晶",
        "unregistered_spirit": "无记名灵基",
    }
    out = ""
    for m in r["resources"]:
        a = m["value"] if m["state"] in ("ready", "partial") else None
        if not a or a["resource"] not in names:
            continue
        k = a["resource"]
        terms = [(a.get("represented_consumed"), "已用"), (a.get("current_inventory"), "库存")]
        if k == "crystal":
            terms = [
                (a.get("active_skill_consumed"), "主动"),
                (a.get("append_skill_consumed"), "追加"),
                (a.get("current_inventory"), "库存"),
            ]
        if k == "unregistered_spirit":
            terms = [(a.get("exchange_count"), "次兑换 × 10"), (a.get("current_inventory"), "库存")]
        formula = '<span class="plus">+</span>'.join(
            f'<span class="v">{num(v)}</span><span class="label">{label}</span>'
            for v, label in terms
        )
        out += f'<div class="resource-row"><div class="resource-icon">{icon(k)}</div><div class="resource-name">{names[k]}</div><div class="resource-formula">{formula}<span class="plus">=</span><span class="sum">{num(a["represented_total"])}</span></div></div>'
    return (
        '<div class="panel resources">'
        + out
        + '<div class="resource-note"><span>使用量依当前可追溯记录计算，非完整终身消耗账本。</span><span class="unknown-pill">大梦火：未提供</span></div></div>'
    )

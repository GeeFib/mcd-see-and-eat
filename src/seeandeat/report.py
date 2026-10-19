"""把搭配结果渲染成人能直接看的东西：Markdown 与单文件 HTML。

HTML 自带样式、无外部依赖，双击就能开，也方便直接分享。
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from .dri import DailyTarget, MEAL_LABEL, MEAL_SPLIT_RANGE
from .planner import Plan, gaps, covered_target, ALL_SLOTS
from .pipeline import PlanContext

# ------------------------------------------------------------------ 达标判定
def status_of(name: str, rate: float, lower_better: bool) -> tuple[str, str]:
    """返回 (文案, 语义色 key)。"""
    if name == "钠":
        if rate <= 1.0:
            return "达标", "ok"
        if rate <= 1.25:
            return "略超", "warn"
        return "超标", "bad"
    if lower_better:
        if rate <= 1.05:
            return "达标", "ok"
        if rate <= 1.20:
            return "略高", "warn"
        return "偏高", "bad"
    # 越高越好型：能量/蛋白质/钙
    if rate >= 0.95:
        return "达标", "ok"
    if rate >= 0.85:
        return "接近", "warn"
    return "不足", "bad"


def pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def fmt(v: float, unit: str) -> str:
    return f"{v:.0f}{unit}" if unit != "kcal" else f"{v:.0f}"


# ------------------------------------------------------------------ Markdown
def render_markdown(ctx: PlanContext, slots: tuple[str, ...] = ALL_SLOTS) -> str:
    t = ctx.target
    covered = covered_target(t, slots)
    lines: list[str] = []
    lines.append("# 🍔 看见什么吃什么 · 今日麦当劳营养搭配")
    lines.append("")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    src = "麦当劳 MCP 实时菜单" if ctx.live else "本地快照（离线演示）"
    lines.append(f"> 门店：**{ctx.store.get('storeName', '-')}**（{ctx.store.get('storeCode', '-')}）　数据源：{src}　生成于 {stamp}")
    lines.append("")

    # 一、目标
    lines.append("## 一、你的每日营养目标")
    lines.append("")
    lines.append("| 项目 | 目标 | 依据 |")
    lines.append("| --- | --- | --- |")
    t_full = t.total
    lines.append(f"| 能量 | {t_full.kcal:.0f} kcal | BMR {t.bmr:.0f} × PAL {t.pal}（{t.pal_desc}），{t.goal_desc} |")
    lines.append(f"| 蛋白质 | {t_full.protein_g:.0f} g | 供能比 {pct(t_full.protein_ratio)}，按 {t.profile.weight_kg:.0f}kg 体重计算 |")
    lines.append(f"| 脂肪 | {t_full.fat_g:.0f} g | 供能比 {pct(t_full.fat_ratio)}（推荐 20%-30%） |")
    lines.append(f"| 碳水化合物 | {t_full.carb_g:.0f} g | 供能比 {pct(t_full.carb_ratio)}（推荐 50%-65%） |")
    lines.append(f"| 钠 | ≤ {t_full.sodium_mg:.0f} mg | 膳食指南：每天食盐不超过 5g |")
    lines.append(f"| 钙 | {t_full.calcium_mg:.0f} mg | DRIs 2023 成人 RNI |")
    lines.append("")
    lines.append("**三餐供能比**（膳食指南 2022）：早餐 25%-30%、午餐 30%-40%、晚餐 30%-35%。")
    lines.append("")

    slot_names = "、".join(MEAL_LABEL[s] for s in slots)
    if len(slots) < 3:
        lines.append(f"> 本次只规划 **{slot_names}**，下表目标按这几餐折算为 {covered.kcal:.0f} kcal。")
        lines.append("")

    # 二、方案
    for idx, plan in enumerate(ctx.plans):
        tag = "ABCDE"[idx]
        lines.append(f"## 二{'' if idx == 0 else '·' + str(idx + 1)}、方案 {tag} · {plan.preset_label}")
        lines.append("")
        lines.append(f"_{plan.preset_desc}_　预估 **¥{plan.price:.1f}**")
        lines.append("")
        for slot in slots:
            b = plan.bundles.get(slot)
            if not b:
                continue
            when = ctx.slot_dates.get(slot, "")
            lines.append(f"### {b.label}" + (f"（{when}）" if when else ""))
            lines.append("")
            lines.append("| 餐品 | 规格 | 能量(kcal) | 蛋白(g) | 脂肪(g) | 碳水(g) | 钠(mg) | 钙(mg) | 价格 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for it in b.items:
                lines.append(
                    f"| {it.name} | {it.size or '—'} | {it.kcal:.0f} | {it.protein:.0f} | "
                    f"{it.fat:.0f} | {it.carb:.0f} | {it.sodium:.0f} | {it.calcium:.0f} | "
                    f"{('¥' + format(it.price, '.1f')) if it.price else '—'} |"
                )
            lines.append(
                f"| **小计** | | **{b.kcal:.0f}** | **{b.protein:.0f}** | **{b.fat:.0f}** | "
                f"**{b.carb:.0f}** | **{b.sodium:.0f}** | **{b.calcium:.0f}** | "
                f"**¥{b.price:.1f}** |"
            )
            share = b.kcal / plan.kcal if plan.kcal else 0
            lo, hi = MEAL_SPLIT_RANGE[slot]
            mark = "✅" if lo <= share <= hi else "⚠️"
            lines.append("")
            lines.append(f"- 占全天能量 {pct(share)}（推荐 {pct(lo)}-{pct(hi)}）{mark}　脂肪供能比 {pct(b.fat_ratio)}　碳水供能比 {pct(b.carb_ratio)}")
            lines.append("")

        g = gaps(plan, t, slots)
        lines.append("**整日达标情况**")
        lines.append("")
        lines.append("| 项目 | 实际 | 目标 | 达成率 | 判定 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name, d in g.items():
            st, _ = status_of(name, d["rate"], d["lower_better"])
            lines.append(
                f"| {name} | {fmt(d['actual'], d['unit'])} | {fmt(d['target'], d['unit'])} | "
                f"{pct(d['rate'])} | {st} |"
            )
        lines.append("")
        codes = [f"{i.name}（编码 {i.code}）" for i in plan.items() if i.code]
        if codes:
            lines.append("<details><summary>下单编码（供 MCP create-order 使用）</summary>")
            lines.append("")
            for c in codes:
                lines.append(f"- {c}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # 三、对比
    if len(ctx.plans) > 1:
        lines.append("## 三、三套方案怎么选")
        lines.append("")
        lines.append("| 方案 | 能量 | 蛋白质 | 脂肪 | 碳水 | 钠 | 钙 | 价格 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for plan in ctx.plans:
            k, p, f, c, na, ca, _ = plan.totals
            lines.append(
                f"| {plan.preset_label} | {k:.0f} | {p:.0f}g | {f:.0f}g | {c:.0f}g | {na:.0f}mg | {ca:.0f}mg | ¥{plan.price:.1f} |"
            )
        lines.append("")

    # 四、诚实提示
    lines.append("## 四、必须说清楚的三件事")
    lines.append("")
    na_lo = min((p.totals[4] for p in ctx.plans), default=0)
    na_hi = max((p.totals[4] for p in ctx.plans), default=0)
    na_range = (f"{na_lo:.0f}mg" if len(ctx.plans) == 1
                else f"{na_lo:.0f}-{na_hi:.0f}mg")
    lines.append(
        f"1. **钠几乎不可能不超标。** 本次方案的钠为 {na_range}，"
        f"最低也有 {na_lo:.0f}mg。麦当劳单个主食钠含量普遍在 500-1200mg，"
        "三餐都吃麦当劳基本会顶到或超过 2000mg 上限。"
        "建议其他餐次多吃新鲜蔬果补钾、全天多喝水。"
    )
    lines.append("2. **膳食纤维与维生素大概率不足。** 麦当劳营养接口只提供能量/蛋白质/脂肪/碳水/钠/钙六项，"
                 "膳食纤维没有数据；实际膳食指南推荐每天 25-30g 膳食纤维，靠麦当劳本身很难吃够，"
                 "请自行补充蔬菜水果。")
    lines.append("3. **这只是一个搭配建议，不是医嘱。** 有高血压、肾病、糖尿病等基础疾病，"
                 "或对能量/钠有严格限制的人群，请以医生和临床营养师的意见为准。")
    lines.append("")

    # 五、未纳入
    if ctx.dropped:
        lines.append("## 五、本次未纳入搭配的餐品")
        lines.append("")
        lines.append(f"共 {len(ctx.dropped)} 项，主要是捆绑套餐（营养无法拆分）和营养表暂未覆盖的限定新品：")
        lines.append("")
        for d in ctx.dropped[:12]:
            lines.append(f"- {d}")
        if len(ctx.dropped) > 12:
            lines.append(f"- …… 其余 {len(ctx.dropped) - 12} 项略")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "<sub>餐品营养数据与在售菜单均来自麦当劳中国 MCP Server（list-nutrition-foods / query-meals / "
        "query-nearby-stores），营养目标依据《中国居民膳食指南（2022）》与《中国居民膳食营养素参考摄入量（2023）》。"
        "本项目为麦当劳程序员节创意开发大赛参赛作品，非麦当劳官方产品，输出仅供参考。</sub>"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ HTML
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e6e8eb;--txt:#1b1f24;--sub:#667085;--ok:#12805c;--warn:#b54708;--bad:#b42318;--brand:#d8232a;--brand-soft:#fdecec}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 60px;background:var(--bg);color:var(--txt);
 font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px}
h2{font-size:19px;margin:34px 0 12px;padding-left:10px;border-left:4px solid var(--brand)}
h3{font-size:16px;margin:20px 0 8px}
.sub{color:var(--sub);font-size:13px;margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{color:var(--sub);font-weight:600;background:#fafbfc}
tr:last-child td{font-weight:700;background:#fafbfc}
.tag{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600}
.ok{color:var(--ok);background:#e7f6ef}.warn{color:var(--warn);background:#fdf2e3}.bad{color:var(--bad);background:#fdeceb}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin:6px 0 4px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .lab{font-size:12px;color:var(--sub)}
.kpi .val{font-size:20px;font-weight:700;margin-top:2px}
.kpi .bar{height:5px;border-radius:99px;background:#eceff2;margin-top:8px;overflow:hidden}
.kpi .bar i{display:block;height:100%;border-radius:99px}
.meal{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 2px}
.chip{background:var(--brand-soft);border:1px solid #f6d2d3;color:#8f1b20;border-radius:8px;padding:6px 11px;font-size:13px}
.chip small{color:#a35b5f;margin-left:6px}
.note{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px 16px;font-size:13.5px;line-height:1.75}
.note ol{margin:6px 0 0;padding-left:20px}
.note li{margin-bottom:7px}
.price{font-size:14px;color:var(--sub)}
details{margin-top:10px;font-size:13px}
details summary{cursor:pointer;color:var(--sub)}
footer{margin-top:34px;color:var(--sub);font-size:12px;line-height:1.7}
.presets{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.preset{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.preset h3{margin:0 0 4px;font-size:16px}
.preset .d{color:var(--sub);font-size:12.5px;min-height:34px}
"""


def render_html(ctx: PlanContext, slots: tuple[str, ...] = ALL_SLOTS) -> str:
    t = ctx.target
    covered = covered_target(t, slots)
    e = escape
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    src = "麦当劳 MCP 实时菜单" if ctx.live else "本地快照（离线演示）"
    parts: list[str] = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>看见什么吃什么 · 麦当劳营养搭配</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
        "<h1>🍔 看见什么吃什么</h1>",
        f"<p class='sub'>门店：{e(str(ctx.store.get('storeName', '-')))}"
        f"（{e(str(ctx.store.get('storeCode', '-')))}）　·　数据源：{src}　·　生成于 {stamp}</p>",
    ]

    # 目标卡片
    tf = t.total
    parts.append("<h2>你的每日营养目标</h2><div class='card'>")
    parts.append(
        f"<div class='price'>身高 {t.profile.height_cm:.0f}cm　体重 {t.profile.weight_kg:.0f}kg　"
        f"年龄 {t.profile.age} 岁　{'男' if t.profile.sex == 'male' else '女'}　"
        f"活动：{e(t.pal_desc)}　目标：{e(t.goal_desc)}</div>"
    )
    parts.append("<div class='grid'>")
    kpis = [
        ("能量", f"{tf.kcal:.0f}", "kcal", None),
        ("蛋白质", f"{tf.protein_g:.0f}", "g", None),
        ("脂肪", f"{tf.fat_g:.0f}", "g", f"供能比 {pct(tf.fat_ratio)}"),
        ("碳水", f"{tf.carb_g:.0f}", "g", f"供能比 {pct(tf.carb_ratio)}"),
        ("钠上限", f"{tf.sodium_mg:.0f}", "mg", "食盐 5g/日"),
        ("钙", f"{tf.calcium_mg:.0f}", "mg", "DRIs RNI"),
    ]
    for lab, val, unit, extra in kpis:
        parts.append(
            f"<div class='kpi'><div class='lab'>{lab}</div>"
            f"<div class='val'>{val}<span style='font-size:12px;color:#667085'> {unit}</span></div>"
            + (f"<div class='lab' style='margin-top:2px'>{extra}</div>" if extra else "")
            + "</div>"
        )
    parts.append("</div>")
    parts.append(
        f"<div class='price' style='margin-top:10px'>BMR {t.bmr:.0f} kcal × PAL {t.pal} = TDEE {t.tdee:.0f} kcal，"
        f"{e(t.goal_desc)}后目标 {tf.kcal:.0f} kcal。三餐供能比按膳食指南：早 25-30%、午 30-40%、晚 30-35%。</div>"
    )
    if len(slots) < 3:
        names = "、".join(MEAL_LABEL[s] for s in slots)
        parts.append(
            f"<div class='price' style='margin-top:6px'>本次只规划 {e(names)}，"
            f"这几餐的目标合计 {covered.kcal:.0f} kcal。</div>"
        )
    parts.append("</div>")

    # 方案
    for idx, plan in enumerate(ctx.plans):
        tag = "ABCDE"[idx]
        parts.append(f"<h2>方案 {tag} · {e(plan.preset_label)}</h2>")
        parts.append("<div class='card'>")
        parts.append(
            f"<div class='price'>{e(plan.preset_desc)}　·　预估 <b style='color:#1b1f24'>¥{plan.price:.1f}</b></div>"
        )
        for slot in slots:
            b = plan.bundles.get(slot)
            if not b:
                continue
            share = b.kcal / plan.kcal if plan.kcal else 0
            lo, hi = MEAL_SPLIT_RANGE[slot]
            ok = lo <= share <= hi
            parts.append(
                f"<h3>{e(b.label)}　<span class='tag {'ok' if ok else 'warn'}'>"
                f"占全天 {pct(share)}</span>　<span style='font-size:12px;color:#667085'>"
                f"{e(str(ctx.slot_dates.get(slot, '')))}</span></h3>"
            )
            parts.append("<div class='meal'>")
            for it in b.items:
                price = f"¥{it.price:.1f}" if it.price else ""
                parts.append(
                    f"<span class='chip'>{e(it.name)}"
                    + (f"<small>{e(it.size)}</small>" if it.size else "")
                    + f"<small>{it.kcal:.0f}kcal · 蛋白{it.protein:.0f}g · 钠{it.sodium:.0f}mg {price}</small>"
                    "</span>"
                )
            parts.append("</div>")
            parts.append("<table><tr><th>餐品</th><th>能量</th><th>蛋白</th><th>脂肪</th><th>碳水</th><th>钠</th><th>钙</th><th>价格</th></tr>")
            for it in b.items:
                parts.append(
                    f"<tr><td>{e(it.name)}</td><td>{it.kcal:.0f}</td><td>{it.protein:.0f}</td>"
                    f"<td>{it.fat:.0f}</td><td>{it.carb:.0f}</td><td>{it.sodium:.0f}</td>"
                    f"<td>{it.calcium:.0f}</td><td>{('¥' + format(it.price, '.1f')) if it.price else '—'}</td></tr>"
                )
            parts.append(
                f"<tr><td>小计</td><td>{b.kcal:.0f}</td><td>{b.protein:.0f}</td><td>{b.fat:.0f}</td>"
                f"<td>{b.carb:.0f}</td><td>{b.sodium:.0f}</td><td>{b.calcium:.0f}</td><td>¥{b.price:.1f}</td></tr>"
            )
            parts.append("</table>")

        g = gaps(plan, t, slots)
        parts.append("<div class='grid' style='margin-top:12px'>")
        for name, d in g.items():
            st, key = status_of(name, d["rate"], d["lower_better"])
            width = min(d["rate"], 1.6) / 1.6 * 100
            color = {"ok": "#12805c", "warn": "#b54708", "bad": "#b42318"}[key]
            parts.append(
                f"<div class='kpi'><div class='lab'>{name}</div>"
                f"<div class='val'>{fmt(d['actual'], d['unit'])}</div>"
                f"<div class='lab'>目标 {fmt(d['target'], d['unit'])} · {pct(d['rate'])}"
                f" <span class='tag {key}'>{st}</span></div>"
                f"<div class='bar'><i style='width:{width:.0f}%;background:{color}'></i></div></div>"
            )
        parts.append("</div>")
        parts.append("</div>")

    # 对比
    if len(ctx.plans) > 1:
        parts.append("<h2>三套方案怎么选</h2><div class='card'><table>")
        parts.append("<tr><th>方案</th><th>能量</th><th>蛋白质</th><th>脂肪</th><th>碳水</th><th>钠</th><th>钙</th><th>价格</th></tr>")
        for plan in ctx.plans:
            k, p, f, c, na, ca, _ = plan.totals
            parts.append(
                f"<tr><td>{e(plan.preset_label)}</td><td>{k:.0f}</td><td>{p:.0f}g</td><td>{f:.0f}g</td>"
                f"<td>{c:.0f}g</td><td>{na:.0f}mg</td><td>{ca:.0f}mg</td><td>¥{plan.price:.1f}</td></tr>"
            )
        parts.append("</table>")
        parts.append("<div class='price'>赶时间就选「均衡日常」；训练日或减脂期选「高蛋白保肌」；"
                     "体检钠偏高、或今天别的两餐也偏咸，就选「低钠轻负担」。</div></div>")

    # 提示
    lo_na = min((p.totals[4] for p in ctx.plans), default=0)
    hi_na = max((p.totals[4] for p in ctx.plans), default=0)
    parts.append(
        "<h2>必须说清楚的三件事</h2><div class='note'><ol>"
        f"<li><b>钠几乎不可能不超标。</b>本次方案的钠为 "
        + (f"{lo_na:.0f}mg" if lo_na == hi_na else f"{lo_na:.0f}-{hi_na:.0f}mg")
        + f"，最低也有 {lo_na:.0f}mg。麦当劳单个主食钠含量普遍 500-1200mg，三餐都吃麦当劳基本会顶到或超过 "
        "2000mg 上限。建议其他餐次多吃新鲜蔬果补钾、全天多喝水。</li>"
        "<li><b>膳食纤维与维生素大概率不足。</b>麦当劳营养接口只提供能量、蛋白质、脂肪、碳水、钠、钙六项，"
        "没有膳食纤维数据；膳食指南推荐每天 25-30g 膳食纤维，靠麦当劳本身很难吃够，请自行补充蔬菜水果。</li>"
        "<li><b>这是搭配建议，不是医嘱。</b>有高血压、肾病、糖尿病等基础疾病，或对能量/钠有严格限制的人群，"
        "请以医生和临床营养师的意见为准。</li>"
        "</ol></div>"
    )

    if ctx.dropped:
        parts.append(f"<h2>本次未纳入搭配的餐品（{len(ctx.dropped)} 项）</h2><div class='card'>")
        parts.append("<div class='price'>主要是捆绑套餐（营养无法拆分）与营养表暂未覆盖的限定新品：</div><ul style='margin:8px 0 0;padding-left:20px;font-size:13px'>")
        for d in ctx.dropped[:12]:
            parts.append(f"<li>{e(d)}</li>")
        if len(ctx.dropped) > 12:
            parts.append(f"<li>…… 其余 {len(ctx.dropped) - 12} 项略</li>")
        parts.append("</ul></div>")

    parts.append(
        "<footer>餐品营养与在售菜单来自麦当劳中国 MCP Server（list-nutrition-foods / query-meals / "
        "query-nearby-stores）；营养目标依据《中国居民膳食指南（2022）》与《中国居民膳食营养素参考摄入量（2023）》。"
        "本项目为麦当劳程序员节创意开发大赛参赛作品，非麦当劳官方产品，输出仅供参考，不构成医疗或营养建议。</footer>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)

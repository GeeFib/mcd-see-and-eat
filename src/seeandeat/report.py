"""把搭配结果渲染成人能直接看的东西：Markdown 与单文件 HTML。

HTML 自带样式、无外部依赖，双击就能开，也方便直接分享。
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from . import anthem
from .dri import DailyTarget, MEAL_LABEL, MEAL_SPLIT_RANGE
from .planner import Plan, gaps, covered_target, ALL_SLOTS
from .pipeline import PlanContext

CN_NUM = "一二三四五六七八九十"


def _sec(n: int) -> str:
    return CN_NUM[n - 1] if 1 <= n <= len(CN_NUM) else str(n)


def price_line(plan: Plan) -> str:
    """价格文案：券的划算方式分两种，不能混着说。"""
    if plan.coupon_hits:
        if plan.coupon_saving > 0:
            head = f"预估 **¥{plan.price:.1f}** → 用券实付 **¥{plan.pay_price:.1f}**（少付 ¥{plan.coupon_saving:.1f}）"
        else:
            head = f"预估 **¥{plan.price:.1f}**（价格不变）"
        if plan.coupon_extra_value > 0:
            head += f"，另多拿价值 ¥{plan.coupon_extra_value:.1f} 的东西"
        return head
    return f"预估 **¥{plan.price:.1f}**"


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


def coupon_rows(ctx: PlanContext) -> list[list[str]]:
    """有券方案的横向对比行。"""
    rows = []
    for plan in ctx.plans:
        if not plan.coupon_hits:
            continue
        titles: list[str] = []
        for h in plan.coupon_hits:
            if h.coupon_title not in titles:
                titles.append(h.coupon_title)
        gain = []
        if plan.coupon_saving > 0:
            gain.append(f"少付 ¥{plan.coupon_saving:.1f}")
        if plan.coupon_extra_value > 0:
            gain.append(f"多拿 ¥{plan.coupon_extra_value:.1f}")
        rows.append([
            plan.preset_label,
            " / ".join(titles),
            f"¥{plan.price:.1f}",
            f"¥{plan.pay_price:.1f}",
            "、".join(gain) or "—",
        ])
    return rows


def reroll_note(ctx: PlanContext) -> str:
    if ctx.reroll >= 3:
        return ("菜单里「既达标又不重样」的组合是有限的：换到第 4 批左右会开始出现重复，"
                "这不是 bug，是池子就这么大。真想彻底换口味，就换门店或换目标（减脂/增肌）。")
    return ""


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
    if ctx.reroll:
        lines.append(f">")
        lines.append(f"> 🎲 **换一批第 {ctx.reroll} 批**：已避开上一批点过的主食，并优先挑当前有券的组合。")
    lines.append("")

    # 一、目标
    lines.append(f"## 一、你的每日营养目标")
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
        lines.append(f"_{plan.preset_desc}_　{price_line(plan)}")
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
        if plan.coupon_hits:
            lines.append(f"**这一套能省的券（{len(plan.coupon_hits)} 处）**")
            lines.append("")
            lines.append("| 餐次 | 餐品 | 券 | 划算方式 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for h in plan.coupon_hits:
                lines.append(
                    f"| {MEAL_LABEL.get(h.slot, h.slot)} | {h.item_name} | {h.coupon_title}"
                    f" | {h.head_line} | {h.note} |"
                )
            lines.append("")
            lines.append(f"- 本套：{plan.coupon_headline}。")
            lines.append(
                "- **买一送一不是打折**：钱一样多，但你多拿到一份。"
                "多出来的那份已经按同规格把能量和钠算给你看了（见「说明」列），"
                "吃不完可以分给同事，别当成免费的空气。"
            )
            lines.append("")
            lines.append("> 券后金额由麦当劳 `calculate-price` 接口带 `couponId` + "
                         "`couponCode` 当场试算，不是估算。")
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

    sec = 3
    # 对比
    if len(ctx.plans) > 1:
        lines.append(f"## {_sec(sec)}、三套方案怎么选")
        sec += 1
        lines.append("")
        lines.append("| 方案 | 能量 | 蛋白质 | 脂肪 | 碳水 | 钠 | 钙 | 价格 | 券后 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for plan in ctx.plans:
            k, p, f, c, na, ca, _ = plan.totals
            if plan.coupon_saving > 0:
                net = f"**¥{plan.pay_price:.1f}**"
            elif plan.coupon_extra_value > 0:
                net = f"价格不变，多拿 ¥{plan.coupon_extra_value:.1f}"
            else:
                net = "—"
            lines.append(
                f"| {plan.preset_label} | {k:.0f} | {p:.0f}g | {f:.0f}g | {c:.0f}g | {na:.0f}mg | {ca:.0f}mg | ¥{plan.price:.1f} | {net} |"
            )
        lines.append("")

    # 有券的组合
    usable = ctx.coupon_items
    if usable or ctx.coupons:
        lines.append(f"## {_sec(sec)}、有券的组合（省钱又不牺牲健康）")
        sec += 1
        lines.append("")
        if usable:
            best = ctx.best_value_plan()
            lines.append(
                f"门店当前有 **{len(ctx.coupons)}** 张券，其中 **{len(usable)}** 个餐品本次搭配用得上："
                + "、".join(f"`{u}`" for u in usable)
            )
            lines.append("")
            rows = coupon_rows(ctx)
            if rows:
                lines.append("| 方案 | 可用券 | 原价 | 实付 | 划算在哪 |")
                lines.append("| --- | --- | --- | --- | --- |")
                for r in rows:
                    lines.append("| " + " | ".join(r) + " |")
                lines.append("")
            if best is not None:
                lines.append(
                    f"> 💰 **要划算又不想动脑子，点「{best.preset_label}」**：{best.coupon_headline}。"
                    f"能量 {best.totals[0]:.0f} kcal、蛋白质 {best.totals[1]:.0f} g、"
                    f"钠 {best.totals[4]:.0f} mg，达标情况和同批其他方案同一档。"
                )
                lines.append("")
            lines.append(
                "「划算法」分两种，别搞混：**特惠/立减**是同样的东西少付钱；"
                "**买一送一/第二份半价**是价格不变但多给你一份 —— 后者省下的钱是「换成实物」给你的，"
                "热量和钠也一起给你了，报告里已经如实算上。"
            )
        else:
            lines.append(
                f"门店当前有 {len(ctx.coupons)} 张券，但都是套餐类/满减类，"
                "对不上这次搭配的单品，所以本次没有可省的钱。"
            )
        lines.append("")

    # 诚实提示
    lines.append(f"## {_sec(sec)}、必须说清楚的三件事")
    sec += 1
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

    # 未纳入
    if ctx.dropped:
        lines.append(f"## {_sec(sec)}、本次未纳入搭配的餐品")
        sec += 1
        lines.append("")
        lines.append(f"共 {len(ctx.dropped)} 项，主要是捆绑套餐（营养无法拆分）和营养表暂未覆盖的限定新品：")
        lines.append("")
        for d in ctx.dropped[:12]:
            lines.append(f"- {d}")
        if len(ctx.dropped) > 12:
            lines.append(f"- …… 其余 {len(ctx.dropped) - 12} 项略")
        lines.append("")

    # 换一批 + 开餐曲
    lines.append(f"## {_sec(sec)}、再来一组（换一批）")
    sec += 1
    lines.append("")
    lines.append("不满意就说一句 **「换一批」**，会换掉上一批点过的主食，"
                 "并优先挑当前门店有券可用的组合（省钱但不牺牲达标度）。")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/plan.py --reroll --sex male --age 28 --height 175 --weight 72")
    lines.append("```")
    lines.append("")
    lines.append(f"开餐曲：运行时会播放「{anthem.SONG_TITLE}」的开场小曲，先把食欲叫起来。"
                 f"默认是原创合成小段（不含第三方旋律）；{anthem.song_hint()}")
    lines.append("")
    if reroll_note(ctx):
        lines.append(f"> {reroll_note(ctx)}")
        lines.append("")
    if ctx.pool_notes:
        lines.append("换一批的取舍记录：")
        lines.append("")
        for n in ctx.pool_notes:
            lines.append(f"- {n}")
        lines.append("")
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
.anthem{position:sticky;top:0;z-index:9;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
 background:linear-gradient(90deg,#1b1f24,#3a2a2c);color:#fff;border-radius:12px;
 padding:12px 16px;margin:0 0 20px;box-shadow:0 6px 18px rgba(27,31,36,.16)}
.anthem .t{font-weight:700;letter-spacing:.5px}
.anthem .t small{font-weight:400;color:#e7c9cb;margin-left:8px;font-size:12px}
.anthem button{appearance:none;border:0;border-radius:99px;padding:8px 16px;cursor:pointer;
 background:var(--brand);color:#fff;font-size:13px;font-weight:600}
.anthem button:hover{background:#b91d23}
.anthem button.playing{background:#12805c}
.coupon{background:#f3fbf7;border:1px solid #cdeadf}
.coupon .save{color:#0f6b4e;font-weight:700}
.chip.promo{background:#e7f6ef;border-color:#bfe4d5;color:#0f6b4e}
.chip.promo small{color:#4a7f6b}
.best{margin-top:10px;padding:12px 14px;border-radius:10px;background:#f3fbf7;border:1px dashed #bfe4d5;font-size:13.5px}
"""

ANTHEM_JS = """
(function(){
  const NOTES=ANTHEM_NOTES, FREQ=ANTHEM_FREQ;
  let ac=null;
  function tone(t0,f,dur,gain){
    const o=ac.createOscillator(), g=ac.createGain();
    o.type='triangle'; o.frequency.value=f;
    g.gain.setValueAtTime(0.0001,t0);
    g.gain.exponentialRampToValueAtTime(gain,t0+0.012);
    g.gain.exponentialRampToValueAtTime(0.0001,t0+dur);
    o.connect(g); g.connect(ac.destination);
    o.start(t0); o.stop(t0+dur+0.02);
  }
  function kick(t0){
    const o=ac.createOscillator(), g=ac.createGain();
    o.type='sine'; o.frequency.setValueAtTime(70,t0);
    o.frequency.exponentialRampToValueAtTime(40,t0+0.16);
    g.gain.setValueAtTime(0.28,t0);
    g.gain.exponentialRampToValueAtTime(0.0001,t0+0.16);
    o.connect(g); g.connect(ac.destination);
    o.start(t0); o.stop(t0+0.18);
  }
  function play(){
    ac = ac || new (window.AudioContext||window.webkitAudioContext)();
    if(ac.state==='suspended'){ac.resume();}
    let t = ac.currentTime + 0.06;
    NOTES.forEach(function(row){
      tone(t, FREQ[row[0]]||440, row[1]*0.96, 0.20);
      t += row[1];
    });
    for(let k=0;k+0.1<t;k+=0.52){ kick(ac.currentTime+0.06+k); }
    return t - ac.currentTime;
  }
  const btn=document.getElementById('anthemBtn');
  const tip=document.getElementById('anthemTip');
  if(btn){ btn.addEventListener('click', function(){
    const dur=play();
    btn.textContent='♪ 正在播放…'; btn.classList.add('playing');
    if(tip){ tip.textContent='原创开餐小段，约 3 秒'; }
    setTimeout(function(){ btn.textContent='▶ 开餐曲'; btn.classList.remove('playing'); }, dur*1000);
  }); }
})();
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
        "<div class='anthem'>",
        "<button id='anthemBtn' type='button'>▶ 开餐曲</button>",
        f"<div class='t'>♪ {e(anthem.SONG_TITLE)}"
        "<small id='anthemTip'>菜单已读，开饭</small></div>",
        "</div>",
        "<h1>🍔 看见什么吃什么</h1>",
        f"<p class='sub'>门店：{e(str(ctx.store.get('storeName', '-')))}"
        f"（{e(str(ctx.store.get('storeCode', '-')))}）　·　数据源：{src}　·　生成于 {stamp}</p>",
    ]
    if ctx.reroll:
        parts.append(
            f"<div class='card' style='border-color:#f6d2d3;background:#fff8f8'>"
            f"🎲 <b>换一批第 {ctx.reroll} 批</b>：已避开上一批点过的主食，"
            f"并优先挑当前有券的组合。看完不满意，再说一句「换一批」。</div>"
        )

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
            f"<div class='price'>{e(plan.preset_desc)}　·　"
            + (
                (
                    f"预估 <b>¥{plan.price:.1f}</b> → 用券实付 <b class='save'>¥{plan.pay_price:.1f}</b>"
                    f"（少付 ¥{plan.coupon_saving:.1f}）"
                    if plan.coupon_saving > 0
                    else f"预估 <b style='color:#1b1f24'>¥{plan.price:.1f}</b>（走券价格不变）"
                )
                + (f"，另多拿价值 ¥{plan.coupon_extra_value:.1f} 的东西"
                   if plan.coupon_extra_value > 0 else "")
                if plan.coupon_hits
                else f"预估 <b style='color:#1b1f24'>¥{plan.price:.1f}</b>"
            )
            + "</div>"
        )
        # 券命中：按餐品名索引，chips 上直接打标
        hit_map: dict[str, Any] = {}
        for h in plan.coupon_hits:
            hit_map.setdefault(h.item_name, h)
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
                h = hit_map.get(it.name)
                cls = "chip promo" if h else "chip"
                promo = (f"<small>🧧 {e(h.coupon_title)}：{e(h.head_line)}</small>") if h else ""
                parts.append(
                    f"<span class='{cls}'>{e(it.name)}"
                    + (f"<small>{e(it.size)}</small>" if it.size else "")
                    + f"<small>{it.kcal:.0f}kcal · 蛋白{it.protein:.0f}g · 钠{it.sodium:.0f}mg {price}</small>"
                    + promo
                    + "</span>"
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
        # 券明细
        if plan.coupon_hits:
            parts.append("<h3>这一套能省的券</h3><div class='card coupon'><table>")
            parts.append("<tr><th>餐次</th><th>餐品</th><th>券</th><th>划算方式</th><th>说明</th></tr>")
            for h in plan.coupon_hits:
                parts.append(
                    f"<tr><td>{e(MEAL_LABEL.get(h.slot, h.slot))}</td><td>{e(h.item_name)}</td>"
                    f"<td>{e(h.coupon_title)}</td><td><b class='save'>{e(h.head_line)}</b></td>"
                    f"<td style='text-align:left;white-space:normal'>{e(h.note)}</td></tr>"
                )
            parts.append("</table>")
            parts.append(f"<div class='price' style='margin-top:8px'>本套：{e(plan.coupon_headline)}。</div>")
            parts.append("<div class='price' style='margin-top:4px'><b>买一送一不是打折</b>："
                         "钱一样多，但你多拿到一份 —— 多出来的那份能量与钠已经算给你看了，"
                         "吃不完分给同事，别当成免费的空气。券后金额由麦当劳 "
                         "<code>calculate-price</code> 接口带 <code>couponId</code> + "
                         "<code>couponCode</code> 当场试算，不是估算。</div>")
            parts.append("</div>")
        parts.append("</div>")

    # 对比
    if len(ctx.plans) > 1:
        parts.append("<h2>三套方案怎么选</h2><div class='card'><table>")
        parts.append("<tr><th>方案</th><th>能量</th><th>蛋白质</th><th>脂肪</th><th>碳水</th>"
                     "<th>钠</th><th>钙</th><th>价格</th><th>券后</th></tr>")
        for plan in ctx.plans:
            k, p, f, c, na, ca, _ = plan.totals
            if plan.coupon_saving > 0:
                net = f"<b class='save'>¥{plan.pay_price:.1f}</b>"
            elif plan.coupon_extra_value > 0:
                net = f"多拿 ¥{plan.coupon_extra_value:.1f}"
            else:
                net = "—"
            parts.append(
                f"<tr><td>{e(plan.preset_label)}</td><td>{k:.0f}</td><td>{p:.0f}g</td><td>{f:.0f}g</td>"
                f"<td>{c:.0f}g</td><td>{na:.0f}mg</td><td>{ca:.0f}mg</td>"
                f"<td>¥{plan.price:.1f}</td><td>{net}</td></tr>"
            )
        parts.append("</table>")
        parts.append("<div class='price'>赶时间就选「均衡日常」；训练日或减脂期选「高蛋白保肌」；"
                     "体检钠偏高、或今天别的两餐也偏咸，就选「低钠轻负担」。</div></div>")

    # 有券的组合
    if ctx.coupon_items or ctx.coupons:
        parts.append("<h2>有券的组合（省钱又不牺牲健康）</h2><div class='card coupon'>")
        if ctx.coupon_items:
            best = ctx.best_value_plan()
            parts.append(
                f"<div class='price'>门店当前有 <b>{len(ctx.coupons)}</b> 张券，其中 "
                f"<b>{len(ctx.coupon_items)}</b> 个餐品本次搭配用得上："
                + "、".join(e(u) for u in ctx.coupon_items) + "</div>"
            )
            rows = coupon_rows(ctx)
            if rows:
                parts.append("<table><tr><th>方案</th><th>可用券</th><th>原价</th>"
                             "<th>实付</th><th>划算在哪</th></tr>")
                for r in rows:
                    parts.append(
                        f"<tr><td>{e(r[0])}</td><td>{e(r[1])}</td><td>{r[2]}</td>"
                        f"<td><b class='save'>{r[3]}</b></td>"
                        f"<td style='text-align:left;white-space:normal'>{e(r[4])}</td></tr>"
                    )
                parts.append("</table>")
            if best is not None:
                parts.append(
                    f"<div class='best'>💰 <b>要划算又不想动脑子，点「{e(best.preset_label)}」</b>："
                    f"{e(best.coupon_headline)}。能量 {best.totals[0]:.0f} kcal、"
                    f"蛋白质 {best.totals[1]:.0f} g、钠 {best.totals[4]:.0f} mg，"
                    f"达标情况和同批其他方案同一档。</div>"
                )
            parts.append(
                "<div class='price' style='margin-top:8px'>「划算法」分两种，别搞混："
                "<b>特惠/立减</b>是同样的东西少付钱；<b>买一送一/第二份半价</b>是价格不变但多给你一份 —— "
                "后者省下的钱是换成实物给你的，热量和钠也一起给你了，上面的表已经如实算上。"
                "下单时 <code>scripts/order.py --use-coupon</code> 会自动带上券商品编码 + "
                "<code>couponId</code> + <code>couponCode</code> 三件套。</div>"
            )
        else:
            parts.append(
                f"<div class='price'>门店当前有 {len(ctx.coupons)} 张券，但都是套餐类/满减类，"
                "对不上这次搭配的单品，所以本次没有可省的钱。</div>"
            )
        parts.append("</div>")

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

    # 换一批 + 开餐曲
    parts.append(
        "<h2>再来一组（换一批）</h2><div class='card'>"
        "<div class='price'>不满意就再说一句 <b>「换一批」</b>：会换掉上一批点过的主食，"
        "并优先挑当前门店有券可用的组合 —— 省钱，但不牺牲达标度。</div>"
        "<div class='price' style='margin-top:6px'>"
        "<code>python scripts/plan.py --reroll --sex male --age 28 --height 175 --weight 72</code>"
        "</div>"
        f"<div class='price' style='margin-top:8px'>开餐曲：运行时播放「{e(anthem.SONG_TITLE)}」的开场小曲，"
        "先把食欲叫起来。页面顶部那个按钮可以再听一次（浏览器策略要求手点，不会自动响）。</div>"
        + (f"<div class='price' style='margin-top:6px'>⚠️ {e(reroll_note(ctx))}</div>"
           if reroll_note(ctx) else "")
        + (
            "<ul style='margin:8px 0 0;padding-left:20px;font-size:13px;color:#667085'>"
            + "".join(f"<li>换一批取舍：{e(n)}</li>" for n in ctx.pool_notes)
            + "</ul>"
            if ctx.pool_notes else ""
        )
        + "</div>"
    )

    parts.append(
        "<footer>餐品营养与在售菜单来自麦当劳中国 MCP Server（list-nutrition-foods / query-meals / "
        "query-nearby-stores / query-store-coupons / calculate-price）；"
        "营养目标依据《中国居民膳食指南（2022）》与《中国居民膳食营养素参考摄入量（2023）》。"
        "开餐曲为原创合成小段，不含第三方旋律；原曲版权归原作者所有，本项目不分发。"
        "本项目为麦当劳程序员节创意开发大赛参赛作品，非麦当劳官方产品，输出仅供参考，不构成医疗或营养建议。</footer>"
    )
    parts.append("<script>" + anthem.notes_js() + ANTHEM_JS + "</script>")
    parts.append("</div></body></html>")
    return "\n".join(parts)

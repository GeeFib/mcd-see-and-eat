#!/usr/bin/env python3
"""「看见什么吃什么」命令行入口。

示例：
    export MCD_MCP_TOKEN=<你的 Token>
    # 175cm / 72kg / 28 岁男性，久坐办公，维持体重，三餐都在麦当劳解决
    python scripts/plan.py --sex male --age 28 --height 175 --weight 72

    # 只在麦当劳解决午餐，减脂
    python scripts/plan.py --sex female --age 30 --height 162 --weight 55 --goal cut --meals lunch

    # 换一批：换一组搭配，并优先挑当前有券的组合
    python scripts/plan.py --sex male --age 28 --height 175 --weight 72 --reroll

    # 不联网，用仓库快照演示
    python scripts/plan.py --offline --html out.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seeandeat import anthem, pipeline, planner, report, state  # noqa: E402
from seeandeat.dri import MEAL_LABEL, Profile  # noqa: E402
from seeandeat.mcp_client import McpError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="plan.py",
        description="看见什么吃什么：按中国居民膳食指南，用麦当劳在售餐品配一日三餐",
    )
    ap.add_argument("--sex", default="male", choices=["male", "female"])
    ap.add_argument("--age", type=int, default=28)
    ap.add_argument("--height", type=float, default=172.0, help="身高 cm")
    ap.add_argument("--weight", type=float, default=68.0, help="体重 kg")
    ap.add_argument("--activity", default="sedentary",
                    choices=["sedentary", "light", "moderate", "active"])
    ap.add_argument("--goal", default="maintain", choices=["cut", "maintain", "bulk"])
    ap.add_argument("--meals", default="breakfast,lunch,dinner",
                    help="哪几餐在麦当劳解决，逗号分隔，如 lunch,dinner")
    ap.add_argument("--preset", default=None,
                    help="只要某一套方案：balanced / high_protein / low_sodium")
    ap.add_argument("--exclude", default="", help="忌口关键词，逗号分隔，如 牛肉,可乐")
    ap.add_argument("--dessert", action="store_true", help="允许加甜品")
    ap.add_argument("--store-code", default=None, help="门店编码")
    ap.add_argument("--keyword", default=None, help="按关键词搜门店")
    ap.add_argument("--offline", action="store_true", help="用本地快照，不请求 MCP")
    # ---- 换一批 / 优惠券 / 开餐曲 ----
    ap.add_argument("--reroll", action="store_true",
                    help="换一批：回避上一批点过的主食，并优先挑有券的组合")
    ap.add_argument("--reset-batch", action="store_true",
                    help="清空「换一批」的批次记忆，从第一批重新开始")
    ap.add_argument("--no-coupon", action="store_true", help="不查优惠券")
    ap.add_argument("--no-sound", action="store_true", help="不播开餐曲")
    ap.add_argument("--silent", action="store_true", help="不打印正文，只出文件")
    # ---- 输出 ----
    ap.add_argument("--json", dest="json_out", default=None, help="结果 JSON 输出路径")
    ap.add_argument("--md", dest="md_out", default=None, help="Markdown 报告输出路径")
    ap.add_argument("--html", dest="html_out", default=None, help="HTML 报告输出路径")
    return ap


def _profile_key(args: argparse.Namespace) -> str:
    return (f"{args.sex}-{args.age}-{args.height:g}-{args.weight:g}-"
            f"{args.activity}-{args.goal}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    slots = tuple(s.strip() for s in args.meals.split(",") if s.strip())
    bad = [s for s in slots if s not in planner.ALL_SLOTS]
    if bad:
        print(f"未知餐次：{bad}，可选 breakfast/lunch/dinner", file=sys.stderr)
        return 2
    if not slots:
        print("至少指定一个餐次", file=sys.stderr)
        return 2

    if args.reset_batch:
        state.clear()
        print("已清空批次记忆，本次按第一批处理。")

    presets = (args.preset,) if args.preset else planner.PlanOptions.presets
    pkey = _profile_key(args)
    hard: tuple[str, ...] = ()
    soft: tuple[str, ...] = ()
    reroll = 0
    if args.reroll:
        h, s, nxt = state.recall(store_code=args.store_code or "", profile_key=pkey)
        if h or s:
            hard, soft, reroll = tuple(h), tuple(s), nxt
            print(f"换一批（第 {reroll} 批）：避让上一批主食 {len(hard)} 项、历史餐品 {len(soft)} 项")
        else:
            print("换一批：没有找到同门店同档案的上一批记录，本次当作第一批。")

    options = planner.PlanOptions(
        allow_dessert=args.dessert,
        presets=tuple(presets),
        slots=slots,
        exclude=tuple(k.strip() for k in args.exclude.split(",") if k.strip()),
        avoid_hard=hard,
        avoid_soft=soft,
        reroll=reroll,
    )

    profile = Profile(
        sex=args.sex, age=args.age, height_cm=args.height,
        weight_kg=args.weight, activity=args.activity, goal=args.goal,
    )

    # 开餐曲：先把食欲叫起来，再上菜。合成/播放失败都不影响配餐。
    sound_note = anthem.play(flag=not args.no_sound)
    if not args.silent:
        print(anthem.BANNER)
        print(f"♪ {sound_note}")

    try:
        ctx = pipeline.run(profile, store_code=args.store_code,
                           keyword=args.keyword, options=options, offline=args.offline,
                           with_coupons=not args.no_coupon)
    except McpError as exc:
        print(f"[MCP 错误] {exc}", file=sys.stderr)
        print("提示：未配置 Token 可加 --offline 用仓库自带快照先体验。", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    ctx.sound_note = sound_note

    # 「到底换掉了什么？」——比只说一句「换一批了」有用得多。
    if ctx.reroll:
        prev = state.previous(store_code=str(ctx.store.get("storeCode") or ""), profile_key=pkey)
        if prev:
            prev_mains = prev.get("mainsBySlot") or {}
            bits: list[str] = []
            for slot in slots:
                now: list[str] = []
                for p in ctx.plans:
                    b = p.bundles.get(slot)
                    if b and b.items and b.items[0].name not in now:
                        now.append(b.items[0].name)
                if not now:
                    continue
                old = set(prev_mains.get(slot) or [])
                fresh = [n for n in now if n not in old]
                carried = [n for n in now if n in old]
                label = MEAL_LABEL.get(slot, slot)
                if fresh:
                    desc = f"{label}换为 " + "、".join(fresh)
                    if carried:
                        desc += f"（{'、'.join(carried)} 沿用）"
                else:
                    desc = f"{label}没换（沿用 {'、'.join(carried)}，池中没有钠相近的替代）"
                bits.append(desc)
            if bits:
                ctx.pool_notes.append("本批主食变化：" + "；".join(bits))

    for w in ctx.warnings:
        print(f"[警告] {w}", file=sys.stderr)
    for n in ctx.pool_notes:
        print(f"[换一批] {n}", file=sys.stderr)
    for n in ctx.coupon_notes:
        print(f"[优惠券] {n}", file=sys.stderr)
    if not ctx.plans:
        print("没有生成任何方案。", file=sys.stderr)
        return 1

    md = report.render_markdown(ctx, slots)
    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")
        print(f"Markdown 已写入 {args.md_out}")
    if args.html_out:
        Path(args.html_out).write_text(report.render_html(ctx, slots), encoding="utf-8")
        print(f"HTML 已写入 {args.html_out}")
    if args.json_out:
        payload = {
            "store": ctx.store,
            "slotDates": ctx.slot_dates,
            "target": ctx.target.to_dict(),
            "reroll": ctx.reroll,
            "sound": sound_note,
            "couponNotes": ctx.coupon_notes,
            "poolNotes": ctx.pool_notes,
            "coupons": {
                "available": len(ctx.coupons),
                "usable": ctx.coupon_items,
                "usableCount": len(ctx.coupon_items),
                "detail": [
                    c.to_dict() for c in ctx.coupons if c.matched_items
                ],
            },
            "plans": [
                {
                    "preset": p.preset,
                    "presetLabel": p.preset_label,
                    "score": round(p.score, 4),
                    "price": round(p.price, 2),
                    "couponSaving": round(p.coupon_saving, 2),
                    "couponExtraValue": round(p.coupon_extra_value, 2),
                    "netPrice": round(p.pay_price, 2),
                    "couponHeadline": p.coupon_headline,
                    "couponHits": [h.to_dict() for h in p.coupon_hits],
                    "totals": {
                        "kcal": p.totals[0], "protein": p.totals[1], "fat": p.totals[2],
                        "carb": p.totals[3], "sodium": p.totals[4], "calcium": p.totals[5],
                    },
                    "meals": {
                        slot: [
                            {"name": it.name, "size": it.size, "code": it.code,
                             "price": it.price, "kcal": it.kcal, "protein": it.protein,
                             "fat": it.fat, "carb": it.carb, "sodium": it.sodium,
                             "calcium": it.calcium}
                            for it in p.bundles[slot].items
                        ]
                        for slot in p.bundles
                    },
                }
                for p in ctx.plans
            ],
            "droppedCount": len(ctx.dropped),
        }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"JSON 已写入 {args.json_out}")

    # 记住这一批，供下次「换一批」避让
    try:
        state.record(
            store_code=str(ctx.store.get("storeCode") or ""),
            slots=slots,
            plans=ctx.plans,
            profile_key=pkey,
            coupons=ctx.coupon_items,
            reroll=ctx.reroll,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 批次记忆写入失败（不影响本次结果）：{exc}", file=sys.stderr)

    if not (args.md_out or args.html_out or args.json_out) and not args.silent:
        print(md)
        print()
        print(anthem.song_hint())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

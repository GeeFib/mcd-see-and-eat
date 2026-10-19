#!/usr/bin/env python3
"""「看见什么吃什么」命令行入口。

示例：
    export MCD_MCP_TOKEN=<你的 Token>
    # 175cm / 72kg / 28 岁男性，久坐办公，维持体重，三餐都在麦当劳解决
    python scripts/plan.py --sex male --age 28 --height 175 --weight 72

    # 只在麦当劳解决午餐，减脂
    python scripts/plan.py --sex female --age 30 --height 162 --weight 55 --goal cut --meals lunch

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

from seeandeat import pipeline, planner, report  # noqa: E402
from seeandeat.dri import Profile  # noqa: E402
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
    ap.add_argument("--json", dest="json_out", default=None, help="结果 JSON 输出路径")
    ap.add_argument("--md", dest="md_out", default=None, help="Markdown 报告输出路径")
    ap.add_argument("--html", dest="html_out", default=None, help="HTML 报告输出路径")
    return ap


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

    presets = (args.preset,) if args.preset else planner.PlanOptions.presets
    options = planner.PlanOptions(
        allow_dessert=args.dessert,
        presets=tuple(presets),
        slots=slots,
        exclude=tuple(k.strip() for k in args.exclude.split(",") if k.strip()),
    )

    profile = Profile(
        sex=args.sex, age=args.age, height_cm=args.height,
        weight_kg=args.weight, activity=args.activity, goal=args.goal,
    )

    try:
        ctx = pipeline.run(profile, store_code=args.store_code,
                           keyword=args.keyword, options=options, offline=args.offline)
    except McpError as exc:
        print(f"[MCP 错误] {exc}", file=sys.stderr)
        print("提示：未配置 Token 可加 --offline 用仓库自带快照先体验。", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    for w in ctx.warnings:
        print(f"[警告] {w}", file=sys.stderr)
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
            "plans": [
                {
                    "preset": p.preset,
                    "presetLabel": p.preset_label,
                    "score": round(p.score, 4),
                    "price": round(p.price, 2),
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

    if not (args.md_out or args.html_out or args.json_out):
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""自检：不需要网络、不需要 Token，用仓库自带的快照跑。

覆盖三件最容易出错、也最容易被"看起来对"骗过去的事：

1. 券与餐品的匹配（20 条用例）—— 宽松匹配一旦过头，就会把「冰美式」的券
   用到「热美式」上，报告里就成了假省钱；
2. 「换一批」的质量守住没有 —— 单看"每批都不一样"是不够的，
   第一版换一批会把全天钠从 2230 顶到 2885mg；
3. 离线全链路能不能出报告。

跑法：
    python scripts/selftest.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seeandeat import coupon, menu as menu_mod, pipeline, planner, state  # noqa: E402
from seeandeat.dri import Profile  # noqa: E402

DATA = ROOT / "data"

# ------------------------------------------------------------------ 1. 券匹配
# (券名或券商品名, 餐品名, 期望能否对上)
COUPON_CASES: list[tuple[str, str, bool]] = [
    ("中薯条买一送一", "中薯条", True),
    ("中薯条买一送一", "小薯条", False),          # 中/小薯条是两个 SKU
    ("大薯条买一送一", "中薯条", False),
    ("可乐买一送一", "可乐中杯", True),
    ("可乐买一送一", "可乐大杯", True),
    ("无糖可乐买一送一", "可乐中杯", False),       # 无糖 ≠ 普通
    ("9.9元中杯冰美式", "热美式中杯", False),      # 冰 ≠ 热
    ("9.9元中杯冰美式", "冰美式中杯", True),
    ("麦咖啡冰美式中杯买一送一", "中杯冰美式", True),  # 品牌词可剥
    ("麦乐鸡5块买一送一", "麦乐鸡5块", True),
    ("麦乐鸡5块买一送一", "麦乐鸡4块", False),     # 5 块 ≠ 4 块
    ("原味板烧鸡腿堡买一送一", "原味板烧鸡腿堡", True),
    ("原味板烧鸡腿堡买一送一", "板烧鸡腿堡", True),  # 命名变体，同一款
    ("双层深海鳕鱼堡买一送一", "深海鳕鱼堡", False),  # 双层 ≠ 单层
    ("双层原味板烧鸡腿麦满分买一送一", "原味板烧鸡腿麦满分", False),
    ("麦辣鸡腿汉堡买一送一", "麦辣鸡腿汉堡", True),
    ("酥酥多笋卷买一送一", "酥酥多笋卷", True),
    ("雪碧买一送一", "可乐中杯", False),           # 牌子都不同
    ("纯悦买一送一", "纯悦", True),
    ("薯条买一送一", "中薯条", True),              # 券不写规格 → 可对上
]

PROFILE = Profile(sex="male", age=28, height_cm=175, weight_kg=72,
                  activity="sedentary", goal="maintain")
SLOTS = ("breakfast", "lunch", "dinner")
REROLL_ROUNDS = 5
# 全天钠的上限：换一批再怎么样也不能比第一批差出一个量级
SODIUM_CEILING = 2700.0


def check_coupons() -> list[str]:
    bad: list[str] = []
    for base, item, want in COUPON_CASES:
        got = coupon._hit(base, item)
        if got != want:
            bad.append(f"券匹配：{base!r} × {item!r} → {got}，期望 {want}")
    return bad


def check_reroll() -> list[str]:
    """连换 REROLL_ROUNDS 批，检查「批内主食不重样」与「钠不失控」。"""
    bad: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        # 用临时目录，别污染用户真实的批次记忆
        import os
        os.environ["SEEANDEAT_STATE_DIR"] = tmp
        try:
            for i in range(1, REROLL_ROUNDS + 1):
                ctx = _offline_ctx(reroll=(i > 1), store_code="1420021")
                if not ctx.plans:
                    bad.append(f"第 {i} 批没有生成任何方案")
                    continue
                for p in ctx.plans:
                    mains = [p.bundles[s].items[0].name
                             for s in SLOTS if p.bundles.get(s) and p.bundles[s].items]
                    dup = len(mains) - len(set(mains))
                    if dup:
                        bad.append(
                            f"第 {i} 批「{p.preset_label}」批内主食重样 {dup} 次：{mains}"
                        )
                    if p.totals[4] > SODIUM_CEILING:
                        bad.append(
                            f"第 {i} 批「{p.preset_label}」钠 {p.totals[4]:.0f}mg "
                            f"超过 {SODIUM_CEILING:.0f}mg 的安全线"
                        )
                state.record(
                    store_code="1420021",
                    slots=SLOTS,
                    plans=ctx.plans,
                    profile_key="selftest",
                    reroll=ctx.reroll,
                )
        finally:
            os.environ.pop("SEEANDEAT_STATE_DIR", None)
    return bad


def _offline_ctx(*, reroll: bool, store_code: str):
    hard, soft, nxt = state.recall(store_code=store_code, profile_key="selftest")
    if not reroll:
        hard, soft, nxt = [], [], 0
    options = planner.PlanOptions(
        avoid_hard=tuple(hard), avoid_soft=tuple(soft), reroll=nxt,
        slots=SLOTS,
    )
    return pipeline.run(PROFILE, store_code=store_code, options=options, offline=True)


def check_pipeline() -> list[str]:
    bad: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        import os
        os.environ["SEEANDEAT_STATE_DIR"] = tmp
        try:
            ctx = _offline_ctx(reroll=False, store_code="1420021")
        finally:
            os.environ.pop("SEEANDEAT_STATE_DIR", None)
    if len(ctx.plans) != 3:
        bad.append(f"离线全链路应给出 3 套方案，实际 {len(ctx.plans)} 套")
    for p in ctx.plans:
        for i, item in enumerate(p.items()):
            if item.kcal <= 0:
                bad.append(f"方案「{p.preset_label}」出现 0 千卡餐品：{item.name}")
    if not ctx.coupons:
        bad.append("快照里应该有门店券，但一张都没读到")
    return bad


def main() -> int:
    problems: list[str] = []
    for name, fn in (("券匹配", check_coupons),
                     ("换一批质量", check_reroll),
                     ("离线全链路", check_pipeline)):
        found = fn()
        status = "OK" if not found else f"FAIL（{len(found)} 处）"
        print(f"[{status}] {name}")
        problems.extend(found)

    if problems:
        print()
        for p in problems:
            print("  ✗", p)
        return 1
    print()
    print(f"全部通过：券匹配 {len(COUPON_CASES)} 条用例，"
          f"换一批连跑 {REROLL_ROUNDS} 批（批内不重样、钠 ≤ {SODIUM_CEILING:.0f}mg）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

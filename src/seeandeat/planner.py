"""三餐组合优化。

思路：
1. 每一餐按「主食 + 可选小食 + 饮品」的角色槽位穷举出候选组合；
2. 用束搜索（beam search）每餐只保留得分最好的若干组合；
3. 三餐再做一次小规模组合，按「整日目标 + 三餐供能比 + 重复度」打分；
4. 输出 3 套侧重不同的方案（均衡 / 高蛋白 / 低钠）。

打分是「偏离目标的加权惩罚」，权重可按预设调整，
因此三套方案不是随机扰动，而是三种明确的取舍。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Iterable, Sequence

from . import taxonomy
from .dri import DailyTarget, NutrientTarget, MEAL_LABEL
from .menu import FoodItem
from .taxonomy import MEAL_SLOTS, DESSERT_CATEGORY


@dataclass
class Weights:
    """各项偏离的惩罚权重，越大表示越不容忍。"""

    kcal: float = 1.0
    protein_short: float = 1.6
    protein_over: float = 0.35
    fat_band: float = 1.0
    carb_band: float = 0.8
    sodium: float = 1.4
    calcium_short: float = 0.8
    split: float = 0.6       # 三餐供能比偏离
    variety: float = 0.8     # 重复度惩罚：一天里点两遍同一个单品很影响食欲


PRESETS: dict[str, tuple[str, str, Weights]] = {
    "balanced": ("均衡日常", "能量、三大营养素、钠钙全面贴近膳食指南", Weights()),
    "high_protein": (
        "高蛋白保肌",
        "优先把蛋白质吃够（减脂/训练日适用），容忍略高的钠",
        Weights(protein_short=3.0, sodium=0.8, calcium_short=1.0),
    ),
    "low_sodium": (
        "低钠轻负担",
        "把钠压到最低（选低钠主食、饮品让位给白水），能量略有浮动可接受",
        Weights(sodium=9.0, kcal=0.5, fat_band=1.0, carb_band=0.5,
                protein_short=1.0, calcium_short=0.6),
    ),
}

FAT_BAND = (0.20, 0.30)
CARB_BAND = (0.45, 0.65)

# 预设除了改权重，还会直接改「目标值」，否则目标太容易达成、三套方案会长得一样。
# 例：高蛋白不是惩罚"蛋白质不足"，而是把蛋白质目标抬到 ~1.35 g/kg。
PRESET_TARGET_SCALE: dict[str, dict[str, float]] = {
    "balanced": {},
    "high_protein": {"protein_g": 1.35},
    "low_sodium": {"sodium_mg": 0.85},
}

ALL_SLOTS = ("breakfast", "lunch", "dinner")


def scaled_target(t: NutrientTarget, scale: dict[str, float]) -> NutrientTarget:
    """按预设缩放单个营养目标。"""
    if not scale:
        return t
    import copy

    nt = copy.copy(t)
    for field_name, factor in scale.items():
        if hasattr(nt, field_name):
            setattr(nt, field_name, getattr(nt, field_name) * factor)
    return nt


def _band_penalty(value: float, lo: float, hi: float) -> float:
    """落在区间内为 0，超出则按超出幅度计惩罚（相对区间宽度归一）。"""
    if value < lo:
        return (lo - value) / (hi - lo)
    if value > hi:
        return (value - hi) / (hi - lo)
    return 0.0


@dataclass
class Bundle:
    """一餐的组合。"""

    slot: str
    items: tuple[FoodItem, ...]
    kcal: float = 0.0
    protein: float = 0.0
    fat: float = 0.0
    carb: float = 0.0
    sodium: float = 0.0
    calcium: float = 0.0
    price: float = 0.0
    score: float = 0.0

    @property
    def label(self) -> str:
        return MEAL_LABEL.get(self.slot, self.slot)

    @property
    def fat_ratio(self) -> float:
        return (self.fat * 9 / self.kcal) if self.kcal > 0 else 0.0

    @property
    def carb_ratio(self) -> float:
        return (self.carb * 4 / self.kcal) if self.kcal > 0 else 0.0

    @property
    def protein_ratio(self) -> float:
        return (self.protein * 4 / self.kcal) if self.kcal > 0 else 0.0


def sum_items(items: Sequence[FoodItem]) -> tuple[float, float, float, float, float, float, float]:
    k = p = f = c = na = ca = price = 0.0
    for it in items:
        k += it.kcal
        p += it.protein
        f += it.fat
        c += it.carb
        na += it.sodium
        ca += it.calcium
        price += it.price or 0.0
    return k, p, f, c, na, ca, price


def score_slot(bundle_nut: tuple[float, ...], target: NutrientTarget, w: Weights) -> float:
    """单餐偏离评分，越小越好。"""
    kcal, protein, fat, carb, sodium, calcium, _ = bundle_nut
    if kcal <= 0:
        return 1e9
    pen = 0.0
    pen += w.kcal * abs(kcal - target.kcal) / max(target.kcal, 1)
    pen += w.protein_short * max(0.0, target.protein_g - protein) / max(target.protein_g, 1)
    pen += w.protein_over * max(0.0, protein - target.protein_g * 1.6) / max(target.protein_g, 1)
    pen += w.fat_band * _band_penalty(fat * 9 / kcal, *FAT_BAND)
    pen += w.carb_band * _band_penalty(carb * 4 / kcal, *CARB_BAND)
    pen += w.sodium * max(0.0, sodium - target.sodium_mg) / max(target.sodium_mg, 1)
    pen += w.calcium_short * max(0.0, target.calcium_mg - calcium) / max(target.calcium_mg, 1)
    return pen


def score_day(
    totals: tuple[float, ...],
    day_target: NutrientTarget,
    slots_kcal: dict[str, float],
    day_kcal: float,
    split_target: dict[str, float],
    w: Weights,
) -> float:
    """整日偏离评分。"""
    kcal, protein, fat, carb, sodium, calcium, _ = totals
    pen = score_slot(totals, day_target, w)
    # 三餐供能比
    for slot, share_target in split_target.items():
        actual = slots_kcal.get(slot, 0) / day_kcal if day_kcal > 0 else 0
        pen += w.split * abs(actual - share_target)
    return pen


@dataclass
class Plan:
    """一整天的方案。"""

    preset: str
    preset_label: str
    preset_desc: str
    bundles: dict[str, Bundle]
    totals: tuple[float, ...]
    score: float
    price: float

    @property
    def kcal(self) -> float:
        return self.totals[0]

    def items(self) -> list[FoodItem]:
        out: list[FoodItem] = []
        for slot in ALL_SLOTS:
            b = self.bundles.get(slot)
            if b:
                out.extend(b.items)
        return out


@dataclass
class PlanOptions:
    beam_width: int = 56          # 每餐保留的候选数
    max_sides: int = 2            # 每餐最多几个小食
    allow_dessert: bool = False   # 是否允许加甜品
    max_plans: int = 3
    presets: tuple[str, ...] = ("balanced", "high_protein", "low_sodium")
    exclude: tuple[str, ...] = ()                 # 用户忌口关键词
    slots: tuple[str, ...] = ALL_SLOTS            # 哪几餐在麦当劳解决


def _candidates_for_slot(
    slot: str,
    pool: list[FoodItem],
    options: PlanOptions,
) -> list[tuple[str, list[FoodItem]]]:
    """按角色槽位取出候选列表：[(角色, [可选餐品])]。"""
    got: dict[str, list[FoodItem]] = {}
    for cat, required in MEAL_SLOTS[slot]:
        items = [f for f in pool if f.category == cat]
        if not items and required:
            return []  # 必需角色无货，这一餐没法配
        got[cat] = items
    return [(cat, got[cat]) for cat, _ in MEAL_SLOTS[slot] if got.get(cat)]


def _filter_exclude(pool: list[FoodItem], exclude: tuple[str, ...]) -> list[FoodItem]:
    if not exclude:
        return pool
    out = []
    for f in pool:
        if any(kw in f.name for kw in exclude):
            continue
        out.append(f)
    return out


def build_bundles(
    slot: str,
    pool: list[FoodItem],
    target: NutrientTarget,
    w: Weights,
    options: PlanOptions,
) -> list[Bundle]:
    """穷举单餐组合并按得分排序，返回前 beam_width 个。"""
    cats = _candidates_for_slot(slot, pool, options)
    if not cats:
        return []

    # 主食必选 1 件；小食类（非主食非饮品）最多 max_sides 件；饮品必选 1 件；甜品可选
    main_cat = cats[0][0]
    optional_cats = [c for c, _ in cats[1:] if c not in ("drink", DESSERT_CATEGORY)]
    drink_cat = "drink" if any(c == "drink" for c, _ in cats) else None

    combos: list[tuple[FoodItem, ...]] = []
    mains = dict(cats)[main_cat]
    drinks = dict(cats).get(drink_cat, []) if drink_cat else []
    opt_lists = [dict(cats)[c] for c in optional_cats]

    side_picks: list[tuple[FoodItem, ...]] = [()]
    for lst in opt_lists:
        side_picks.extend((x,) for x in lst)
    if options.max_sides >= 2 and len(opt_lists) >= 2:
        for a, b in product(opt_lists[0], opt_lists[1]):
            if a.category != b.category or a.name != b.name:
                side_picks.append((a, b))
    side_picks = side_picks[: 1 + sum(len(l) for l in opt_lists) + 400]

    dessert_items: list[FoodItem] = []
    if options.allow_dessert:
        dessert_items = [None] + [f for f in pool if f.category == DESSERT_CATEGORY]  # type: ignore[list-item]

    for main in mains:
        for sides in side_picks:
            base = (main,) + sides
            variants: list[tuple[FoodItem, ...]] = []
            if drinks:
                variants.extend(base + (d,) for d in drinks)
            else:
                variants.append(base)
            if dessert_items:
                variants = [v + (d,) for v in variants for d in dessert_items if d is not None] + variants
            combos.extend(variants)

    bundles: list[Bundle] = []
    for combo in combos:
        nut = sum_items(combo)
        score = score_slot(nut, target, w)
        bundles.append(
            Bundle(slot=slot, items=tuple(combo), kcal=nut[0], protein=nut[1], fat=nut[2],
                   carb=nut[3], sodium=nut[4], calcium=nut[5], price=nut[6], score=score)
        )
    bundles.sort(key=lambda b: b.score)

    # 能量带过滤：单餐先落在「目标 ±25%」内，再谈其他营养素，
    # 否则优化器会为了压脂肪比而给出偏少的一餐。
    lo, hi = taxonomy.KCAL_BAND
    in_band = [
        b for b in bundles
        if target.kcal * lo <= b.kcal <= target.kcal * hi
    ]
    ordered = in_band if len(in_band) >= options.beam_width else bundles

    seen: set[tuple[str, ...]] = set()
    uniq: list[Bundle] = []
    for b in ordered:
        key = tuple(sorted(i.name for i in b.items))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)
        if len(uniq) >= options.beam_width:
            break
    return uniq


def make_plans(
    pools: dict[str, list[FoodItem]],
    target: DailyTarget,
    options: PlanOptions | None = None,
) -> list[Plan]:
    """生成若干套整日方案。"""
    options = options or PlanOptions()
    pools = {k: _filter_exclude(v, options.exclude) for k, v in pools.items()}

    split_target = {s: target.meals[s].kcal / target.total.kcal for s in target.meals}

    slots = tuple(s for s in options.slots if s in ALL_SLOTS) or ALL_SLOTS
    # 只规划部分餐次时，整日目标按这些餐次的供能比折算
    covered = sum(target.meals[s].kcal for s in slots)
    day_target = NutrientTarget(
        kcal=covered,
        protein_g=sum(target.meals[s].protein_g for s in slots),
        fat_g=sum(target.meals[s].fat_g for s in slots),
        carb_g=sum(target.meals[s].carb_g for s in slots),
        sodium_mg=target.total.sodium_mg * (covered / target.total.kcal if target.total.kcal else 1),
        calcium_mg=sum(target.meals[s].calcium_mg for s in slots),
        protein_ratio=target.total.protein_ratio,
        fat_ratio=target.total.fat_ratio,
        carb_ratio=target.total.carb_ratio,
    )
    split_target = {s: target.meals[s].kcal / covered for s in slots} if covered else {}

    plans: list[Plan] = []
    for preset in options.presets:
        if preset not in PRESETS:
            continue
        label, desc, w = PRESETS[preset]
        scale = PRESET_TARGET_SCALE.get(preset, {})

        per_slot: dict[str, list[Bundle]] = {}
        for slot in slots:
            st = scaled_target(target.meals[slot], scale)
            per_slot[slot] = build_bundles(slot, pools.get(slot, []), st, w, options)
        if not all(per_slot[s] for s in per_slot):
            continue

        preset_day_target = scaled_target(day_target, scale)
        best: list[Plan] = []
        for combo in product(*[per_slot[s][: options.beam_width] for s in slots]):
            bundles = dict(zip(slots, combo))
            all_items: tuple[FoodItem, ...] = ()
            for b in combo:
                all_items += b.items
            totals = sum_items(all_items)
            day_kcal = totals[0]
            slots_kcal = {s: bundles[s].kcal for s in slots}
            names = [i.name for i in all_items]
            dup = len(names) - len(set(names))
            pen = score_day(totals, preset_day_target, slots_kcal, day_kcal, split_target, w)
            pen += options_variety(options) * dup
            best.append(
                Plan(preset=preset, preset_label=label, preset_desc=desc,
                     bundles=bundles, totals=totals, score=pen, price=totals[6])
            )
        best.sort(key=lambda p: p.score)
        plans.extend(best[: 1])

    # 去掉三套方案内容完全相同的情形
    uniq: list[Plan] = []
    sigs: set[tuple] = set()
    for p in plans:
        sig = tuple(sorted(i.name for i in p.items()))
        if sig in sigs:
            continue
        sigs.add(sig)
        uniq.append(p)
    return uniq[: options.max_plans]


def options_variety(options: PlanOptions) -> float:
    return PRESETS["balanced"][2].variety


def covered_target(target: DailyTarget, slots: tuple[str, ...]) -> NutrientTarget:
    """只规划部分餐次时对应的折算目标（供报告对比用）。"""
    slots = tuple(s for s in slots if s in ALL_SLOTS) or ALL_SLOTS
    if len(slots) == len(ALL_SLOTS):
        return target.total  # 三餐全规划时直接用整日目标，避免分餐取整带来偏差
    covered = sum(target.meals[s].kcal for s in slots)
    return NutrientTarget(
        kcal=covered,
        protein_g=sum(target.meals[s].protein_g for s in slots),
        fat_g=sum(target.meals[s].fat_g for s in slots),
        carb_g=sum(target.meals[s].carb_g for s in slots),
        sodium_mg=target.total.sodium_mg * (covered / target.total.kcal if target.total.kcal else 1),
        calcium_mg=sum(target.meals[s].calcium_mg for s in slots),
    )


def gaps(plan: Plan, target: DailyTarget, slots: tuple[str, ...] = ALL_SLOTS) -> dict[str, dict[str, float]]:
    """方案与目标的差额：「实际 / 目标 / 达成率」。"""
    kcal, protein, fat, carb, sodium, calcium, _ = plan.totals
    t = covered_target(target, slots)
    return {
        "能量": {"actual": kcal, "target": t.kcal, "unit": "kcal",
                 "rate": kcal / t.kcal if t.kcal else 0, "lower_better": False},
        "蛋白质": {"actual": protein, "target": t.protein_g, "unit": "g",
                   "rate": protein / t.protein_g if t.protein_g else 0, "lower_better": False},
        "脂肪": {"actual": fat, "target": t.fat_g, "unit": "g",
                 "rate": fat / t.fat_g if t.fat_g else 0, "lower_better": True},
        "碳水化合物": {"actual": carb, "target": t.carb_g, "unit": "g",
                       "rate": carb / t.carb_g if t.carb_g else 0, "lower_better": True},
        "钠": {"actual": sodium, "target": t.sodium_mg, "unit": "mg",
               "rate": sodium / t.sodium_mg if t.sodium_mg else 0, "lower_better": True},
        "钙": {"actual": calcium, "target": t.calcium_mg, "unit": "mg",
               "rate": calcium / t.calcium_mg if t.calcium_mg else 0, "lower_better": False},
    }

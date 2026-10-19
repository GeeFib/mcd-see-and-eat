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

from dataclasses import dataclass, field, replace
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

# 「换一批」的底线：换主食可以，但替代主食的钠不能比原主食高出这个倍数
# （池子就这么大，硬换的结果是把「低钠」方案换成高钠方案，那就本末倒置了）
REROLL_SODIUM_TOLERANCE = 1.15


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
    # 优惠券相关（由 coupon.attach_to_plans 回填，没有券时为空/等于原价）
    coupon_hits: list = field(default_factory=list)
    coupon_saving: float = 0.0        # 现金少付
    coupon_extra_value: float = 0.0   # 同价多拿到的价值（买一送一）
    net_price: float = 0.0

    @property
    def kcal(self) -> float:
        return self.totals[0]

    @property
    def pay_price(self) -> float:
        """券后实付（没有券时就是原价）。"""
        return self.net_price if self.net_price > 0 else self.price

    @property
    def coupon_summary(self) -> str:
        if not self.coupon_hits:
            return ""
        titles = []
        for h in self.coupon_hits:
            if h.coupon_title not in titles:
                titles.append(h.coupon_title)
        return " + ".join(titles)

    @property
    def coupon_headline(self) -> str:
        """一句话说清这套方案用券后到底怎么个划算法。"""
        if not self.coupon_hits:
            return ""
        bits: list[str] = []
        if self.coupon_saving > 0:
            bits.append(f"实付 ¥{self.pay_price:.1f}（少付 ¥{self.coupon_saving:.1f}）")
        if self.coupon_extra_value > 0:
            bits.append(f"价格不变，多拿价值 ¥{self.coupon_extra_value:.1f} 的东西")
        return "；".join(bits)

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
    # ---- 「换一批」相关 ----
    avoid_hard: tuple[str, ...] = ()   # 上一批点过的主食，本批必须换掉
    avoid_soft: tuple[str, ...] = ()   # 前几批出现过的餐品，本批尽量换掉
    avoid_penalty: float = 0.12        # 「尽量换掉」的每项扣分
    reroll: int = 0                    # 第几批（0 = 首批）
    avoid_sodium_floor: dict = field(default_factory=dict)  # 餐次 → 被避让主食的最低钠
    reroll_sodium_weight: float = 3.0  # 替代主食钠超基准时的扣分权重
    reroll_sodium_weight_low_na: float = 6.0  # 低钠预设对钠更敏感，扣得更狠
    variety_reroll: float = 2.2        # 换一批时更不容忍「批内同款点两遍」
    # ---- 优惠券相关 ----
    coupon_items: tuple[str, ...] = ()  # 当前门店有券可用的餐品名
    coupon_bonus: float = 0.22          # 命中券的加分（体现为等额降分）


def _extras_penalty(items: Sequence[FoodItem], options: PlanOptions) -> float:
    """回避上一批 + 优惠券加持，统一折算成扣分项（可正可负）。

    关键取舍：有券的餐品**不做回避**。用户要「换一批 + 优先有券的组合」，
    这时候因为上一批点过就把券品排除掉，等于自己把钱扔掉。
    """
    pen = 0.0
    coupon = set(options.coupon_items)
    soft = set(options.avoid_soft)
    for i in items:
        if i.name in coupon:
            pen -= options.coupon_bonus
        elif i.name in soft:
            pen += options.avoid_penalty
    return pen


def required_categories(slot: str) -> set[str]:
    return {cat for cat, required in MEAL_SLOTS[slot] if required}


def _main_category(slot: str) -> str | None:
    cats = MEAL_SLOTS.get(slot) or ()
    return cats[0][0] if cats else None


def reroll_sodium_floors(
    pools: dict[str, list[FoodItem]],
    options: PlanOptions,
) -> dict[str, float]:
    """「换一批」的钠基准线：每个餐次被避让主食里钠最低的那个。

    有了基准线，替代主食的钠一旦明显超过它，就在打分阶段被扣分——
    这样优化器会优先挑「同样不重样、但钠差不多」的替代品。
    """
    floors: dict[str, float] = {}
    if not options.avoid_hard:
        return floors
    avoid = set(options.avoid_hard)
    for slot, pool in pools.items():
        main_cat = _main_category(slot)
        if not main_cat:
            continue
        avoided = [f for f in pool if f.category == main_cat and f.name in avoid]
        if avoided:
            floors[slot] = min(f.sodium for f in avoided)
    return floors


def _reroll_sodium_penalty(
    slot: str,
    items: Sequence[FoodItem],
    options: PlanOptions,
    weight: float,
) -> float:
    """替代主食的钠相对基准的超额扣分（相对幅度，越大越不容忍）。"""
    floor = options.avoid_sodium_floor.get(slot)
    if not floor or weight <= 0:
        return 0.0
    main_cat = _main_category(slot)
    threshold = floor * REROLL_SODIUM_TOLERANCE
    for it in items:
        if it.category == main_cat and it.sodium > threshold:
            return weight * (it.sodium - threshold) / max(floor, 1.0)
        if it.category == main_cat:
            return 0.0
    return 0.0


def _sodium_guard(
    slot: str,
    full: list[FoodItem],
    kept: list[FoodItem],
    avoid: set[str],
    notes: list[str],
) -> list[FoodItem]:
    """「换一批」的钠档位保护。

    换主食是本功能的卖点，但不能为了「不重样」把这一餐的钠换高一大截。
    规则：把被避让的主食里钠最低的那个当基准，如果留下的主食里
    **没有任何一个**落在基准 × REROLL_SODIUM_TOLERANCE 之内，
    说明这个餐次根本换不动健康的，就把基准那个放回去，并如实说明。
    """
    main_cat = _main_category(slot)
    if not main_cat:
        return kept
    avoided_mains = [f for f in full if f.category == main_cat and f.name in avoid]
    if not avoided_mains:
        return kept
    kept_mains = [f for f in kept if f.category == main_cat]
    threshold = min(f.sodium for f in avoided_mains) * REROLL_SODIUM_TOLERANCE
    if kept_mains and min(f.sodium for f in kept_mains) <= threshold:
        return kept
    back = min(avoided_mains, key=lambda f: f.sodium)
    if any(f.name == back.name for f in kept):
        return kept
    notes.append(
        f"{slot}：本批候选里没有钠含量相近的替代主食，「{back.name}」"
        f"（{back.sodium:.0f} mg 钠）仍在可选之列——硬换会明显抬高这一餐的钠"
    )
    return kept + [back]


def filter_pools(
    pools: dict[str, list[FoodItem]],
    options: PlanOptions,
) -> tuple[dict[str, list[FoodItem]], list[str]]:
    """忌口硬过滤 + 「换一批」的主食回避。

    主食回避会保住两件事：
    1. 可行性——某个必需角色被避让清空了，就放回一个（宁可重复，也不能配不出这餐）；
    2. 营养——留下的主食里没有钠含量相近的替代，就把最低钠的那个被避让主食放回来
       （宁可重样，也不能把「低钠」方案换成一个高钠方案）。
    """
    out: dict[str, list[FoodItem]] = {}
    notes: list[str] = []
    for slot, pool in pools.items():
        items = _filter_exclude(pool, options.exclude)
        if options.avoid_hard:
            need = required_categories(slot)
            avoid = set(options.avoid_hard)
            full = items
            kept = [f for f in full if f.name not in avoid]
            # 某个必需角色被避让清空了？按原顺序放回一个，宁可重复也不能配不出这餐
            for cat in need:
                if any(f.category == cat for f in kept):
                    continue
                fallback = next((f for f in full if f.category == cat), None)
                if fallback is not None:
                    kept.append(fallback)
                    notes.append(
                        f"{slot}：{fallback.category_label}只剩「{fallback.name}」可选，本批保留"
                    )
            kept = _sodium_guard(slot, full, kept, avoid, notes)
            items = kept
        out[slot] = items
    return out, notes


def _filter_exclude(pool: list[FoodItem], exclude: tuple[str, ...]) -> list[FoodItem]:
    if not exclude:
        return pool
    out = []
    for f in pool:
        if any(kw in f.name for kw in exclude):
            continue
        out.append(f)
    return out


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


def build_bundles(
    slot: str,
    pool: list[FoodItem],
    target: NutrientTarget,
    w: Weights,
    options: PlanOptions,
    sodium_weight: float = 0.0,
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
        score = (
            score_slot(nut, target, w)
            + _extras_penalty(combo, options)
            + _reroll_sodium_penalty(slot, combo, options, sodium_weight)
        )
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

    # 分层取样：主食是「换一批」的主角。如果让某个低钠主食按分数把所有名额占满，
    # 束搜索里就再没有别的汉堡可选，午晚餐只能重样——重复度惩罚也就无从生效。
    # 所以每个主食先各取若干，再合并按分数排序。
    quota = max(4, options.beam_width // max(1, len(mains)))
    buckets: dict[str, list[Bundle]] = {}
    for b in ordered:
        key = b.items[0].name if b.items else ""
        bucket = buckets.setdefault(key, [])
        if len(bucket) < quota:
            bucket.append(b)
    picked: set[int] = set()
    merged: list[Bundle] = []
    for bucket in buckets.values():
        for b in bucket:
            merged.append(b)
            picked.add(id(b))
    merged.sort(key=lambda b: b.score)
    for b in ordered:  # 名额没占满就用剩下的补齐
        if len(merged) >= options.beam_width:
            break
        if id(b) not in picked:
            merged.append(b)
            picked.add(id(b))
    merged.sort(key=lambda b: b.score)

    seen: set[tuple[str, ...]] = set()
    uniq: list[Bundle] = []
    for b in merged:
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
    notes_out: list[str] | None = None,
) -> list[Plan]:
    """生成若干套整日方案。"""
    options = options or PlanOptions()
    if options.avoid_hard and not options.avoid_sodium_floor:
        options = replace(options, avoid_sodium_floor=reroll_sodium_floors(pools, options))
    pools, pool_notes = filter_pools(pools, options)
    if notes_out is not None:
        notes_out.extend(pool_notes)

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
        na_w = (
            options.reroll_sodium_weight_low_na
            if preset == "low_sodium"
            else options.reroll_sodium_weight
        )
        for slot in slots:
            st = scaled_target(target.meals[slot], scale)
            per_slot[slot] = build_bundles(
                slot, pools.get(slot, []), st, w, options, sodium_weight=na_w
            )
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
            main_names = [b.items[0].name for s in slots if (b := bundles.get(s)) and b.items]
            main_dup = len(main_names) - len(set(main_names))
            pen = score_day(totals, preset_day_target, slots_kcal, day_kcal, split_target, w)
            # 换一批时把「批内同款点两遍」看得更重，主食重样再加一倍：
            # 低钠池本来就窄，不加重的话优化器会为了压钠让午晚餐都点同一只汉堡。
            variety_w = options.variety_reroll if options.reroll else options_variety(options)
            pen += variety_w * (dup + 2 * main_dup)
            # 「换一批」的钠基准线在选批阶段同样要算一遍：
            # 只在入束时扣分的话，束里的高钠主食会在选批时靠能量贴合度翻盘。
            for s in slots:
                b = bundles.get(s)
                if b:
                    pen += _reroll_sodium_penalty(s, b.items, options, na_w)
            pen += _extras_penalty(all_items, options)
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

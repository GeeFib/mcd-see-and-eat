"""每日营养目标计算。

依据：
- 《中国居民膳食指南（2022）》平衡膳食八准则 / 三餐能量分配
- 《中国居民膳食营养素参考摄入量（2023）》(DRIs)：能量 EAR、蛋白质 RNI、钙 RNI、钠 AI 与 PI-NCD
- Mifflin-St Jeor 公式估基础代谢 (BMR)

所有数值仅用于膳食搭配参考，不构成医疗建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- 活动水平 (PAL)
# DRIs 2023 对成人身体活动水平分级：低 1.50 / 中 1.75 / 高 2.00。
# 额外补一个 1.40："久坐办公 / 几乎不运动"，贴合程序员等真实人群。
PAL_LEVELS = {
    "sedentary": (1.40, "久坐办公（几乎不运动，典型打工人）"),
    "light": (1.50, "轻体力活动（DRIs 低活动水平，日常通勤+偶尔走动）"),
    "moderate": (1.75, "中体力活动（DRIs 中等，每周规律运动 3-5 次）"),
    "active": (2.00, "重体力活动（DRIs 高活动水平，体力工作或高强度训练）"),
}

GOALS = {
    "cut": ("减脂", 0.82),
    "maintain": ("维持体重", 1.00),
    "bulk": ("增肌", 1.10),
}

# 每公斤体重蛋白质目标 (g/kg·d)
PROTEIN_PER_KG = {"cut": 1.35, "maintain": 1.00, "bulk": 1.55}

# 碳水化合物供能比目标
CARB_RATIO = {"cut": 0.50, "maintain": 0.55, "bulk": 0.58}

FAT_RANGE = (0.20, 0.30)          # 脂肪供能比可接受区间（DRIs / 膳食指南）
PROTEIN_RANGE = (0.10, 0.20)      # 蛋白质供能比可接受区间
CARB_RANGE = (0.50, 0.65)         # 碳水化合物供能比可接受区间

# 三餐能量分配（膳食指南 2022：早餐 25-30%、午餐 30-40%、晚餐 30-35%）
MEAL_SPLIT = {"breakfast": 0.28, "lunch": 0.38, "dinner": 0.34}
MEAL_SPLIT_RANGE = {
    "breakfast": (0.25, 0.30),
    "lunch": (0.30, 0.40),
    "dinner": (0.30, 0.35),
}
MEAL_LABEL = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}


@dataclass
class Profile:
    """用户画像。"""

    sex: str = "male"            # male | female
    age: int = 28
    height_cm: float = 172.0
    weight_kg: float = 68.0
    activity: str = "sedentary"  # sedentary | light | moderate | active
    goal: str = "maintain"       # cut | maintain | bulk

    def validate(self) -> None:
        if self.sex not in ("male", "female"):
            raise ValueError("sex 只能是 male 或 female")
        if self.activity not in PAL_LEVELS:
            raise ValueError(f"activity 只能是 {list(PAL_LEVELS)}")
        if self.goal not in GOALS:
            raise ValueError(f"goal 只能是 {list(GOALS)}")
        if not (140 <= self.height_cm <= 210):
            raise ValueError("身高需在 140-210 cm 之间")
        if not (35 <= self.weight_kg <= 180):
            raise ValueError("体重需在 35-180 kg 之间")
        if not (12 <= self.age <= 90):
            raise ValueError("年龄需在 12-90 岁之间")


@dataclass
class NutrientTarget:
    """每日 / 每餐营养目标。"""

    kcal: float = 0.0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carb_g: float = 0.0
    sodium_mg: float = 2000.0     # 上限：盐 5g/d ≈ 钠 2000mg（膳食指南）
    calcium_mg: float = 800.0     # 目标：DRIs RNI
    # 供能比
    protein_ratio: float = 0.0
    fat_ratio: float = 0.0
    carb_ratio: float = 0.0


@dataclass
class DailyTarget:
    profile: Profile
    bmr: float
    tdee: float
    pal: float
    pal_desc: str
    goal_desc: str
    total: NutrientTarget
    meals: dict[str, NutrientTarget] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def bmr_mifflin(profile: Profile) -> float:
    """Mifflin-St Jeor 基础代谢公式。"""
    w, h, a = profile.weight_kg, profile.height_cm, profile.age
    base = 10 * w + 6.25 * h - 5 * a
    return base + 5 if profile.sex == "male" else base - 161


def bmi_of(profile: Profile) -> float:
    m = profile.height_cm / 100
    return profile.weight_kg / (m * m)


def bmi_comment(bmi: float) -> str:
    # 中国成人 BMI 标准：18.5-23.9 正常，24-27.9 超重，≥28 肥胖
    if bmi < 18.5:
        return "偏瘦（<18.5）"
    if bmi < 24:
        return "正常（18.5-23.9）"
    if bmi < 28:
        return "超重（24-27.9）"
    return "肥胖（≥28）"


def calcium_rni(profile: Profile) -> float:
    """DRIs 2023 成人钙 RNI：18-49 岁 800mg，50 岁及以上 1000mg。"""
    return 1000.0 if profile.age >= 50 else 800.0


def protein_rni(profile: Profile) -> float:
    """DRIs 2023 成人蛋白质 RNI：男 65g/d，女 55g/d（18 岁及以上）。"""
    return 65.0 if profile.sex == "male" else 55.0


def _macros(kcal: float, protein_g: float, carb_ratio: float) -> tuple[float, float, float, float, float, float]:
    """在供能比约束内配平三大营养素。返回 (protein_g, fat_g, carb_g, p_ratio, f_ratio, c_ratio)。"""
    protein_kcal = protein_g * 4
    # 蛋白质供能比封顶 20%
    if protein_kcal / kcal > PROTEIN_RANGE[1]:
        protein_kcal = kcal * PROTEIN_RANGE[1]
        protein_g = protein_kcal / 4
    carb_kcal = kcal * carb_ratio
    fat_kcal = kcal - protein_kcal - carb_kcal
    # 脂肪供能比必须落在 20%-30%，超出就把余量还给碳水
    lo, hi = FAT_RANGE
    if fat_kcal / kcal < lo:
        fat_kcal = kcal * lo
        carb_kcal = kcal - protein_kcal - fat_kcal
    elif fat_kcal / kcal > hi:
        fat_kcal = kcal * hi
        carb_kcal = kcal - protein_kcal - fat_kcal
    # 碳水兜底封顶 65%
    if carb_kcal / kcal > CARB_RANGE[1]:
        carb_kcal = kcal * CARB_RANGE[1]
        fat_kcal = kcal - protein_kcal - carb_kcal
    return (
        round(protein_g, 1),
        round(fat_kcal / 9, 1),
        round(carb_kcal / 4, 1),
        round(protein_kcal / kcal, 3),
        round(fat_kcal / kcal, 3),
        round(carb_kcal / kcal, 3),
    )


def compute_target(profile: Profile) -> DailyTarget:
    """算出每日总目标 + 三餐拆分目标。"""
    profile.validate()
    pal, pal_desc = PAL_LEVELS[profile.activity]
    goal_desc, factor = GOALS[profile.goal]

    bmr = bmr_mifflin(profile)
    tdee = bmr * pal
    kcal = tdee * factor
    # 减脂也不低于 BMR，避免给出过低能量
    if kcal < bmr:
        kcal = bmr

    protein_g = max(profile.weight_kg * PROTEIN_PER_KG[profile.goal], protein_rni(profile) * 0.9)
    carb_ratio = CARB_RATIO[profile.goal]
    protein_g, fat_g, carb_g, p_ratio, f_ratio, c_ratio = _macros(kcal, protein_g, carb_ratio)

    total = NutrientTarget(
        kcal=round(kcal),
        protein_g=protein_g,
        fat_g=fat_g,
        carb_g=carb_g,
        calcium_mg=calcium_rni(profile),
        sodium_mg=2000.0,
        protein_ratio=p_ratio,
        fat_ratio=f_ratio,
        carb_ratio=c_ratio,
    )

    meals: dict[str, NutrientTarget] = {}
    for slot, share in MEAL_SPLIT.items():
        mk = kcal * share
        # 蛋白质按餐均分略有侧重：早/午/晚 0.9 / 1.1 / 1.0 倍
        w = {"breakfast": 0.9, "lunch": 1.1, "dinner": 1.0}[slot]
        mp = protein_g * share * w
        mp_g, mf_g, mc_g, pr, fr, cr = _macros(mk, mp, carb_ratio)
        meals[slot] = NutrientTarget(
            kcal=round(mk),
            protein_g=mp_g,
            fat_g=mf_g,
            carb_g=mc_g,
            calcium_mg=round(calcium_rni(profile) * share),
            sodium_mg=2000.0 * share,
            protein_ratio=pr,
            fat_ratio=fr,
            carb_ratio=cr,
        )

    bmi = bmi_of(profile)
    notes = [
        f"BMI {bmi:.1f}，{bmi_comment(bmi)}",
        f"BMR {bmr:.0f} kcal × PAL {pal}（{pal_desc}）= TDEE {tdee:.0f} kcal，"
        f"{goal_desc}后每日目标 {total.kcal:.0f} kcal",
        "钠上限 2000mg/日（相当于食盐 5g，《中国居民膳食指南 2022》推荐上限）",
        f"钙目标 {total.calcium_mg:.0f}mg/日（DRIs 2023 成人 RNI）",
        "三餐供能比按膳食指南推荐：早餐 25-30%、午餐 30-40%、晚餐 30-35%",
    ]
    return DailyTarget(
        profile=profile,
        bmr=round(bmr),
        tdee=round(tdee),
        pal=pal,
        pal_desc=pal_desc,
        goal_desc=goal_desc,
        total=total,
        meals=meals,
        notes=notes,
    )

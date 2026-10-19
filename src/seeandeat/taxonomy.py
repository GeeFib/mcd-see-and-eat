"""餐品分类与时段适配规则。

麦当劳菜单更新频繁，这里用「关键词规则 + 显式例外表」而不是硬编码全量名单，
新餐品进来能自动落到大致正确的格子，个别例外再手动补。

分类（对应平衡膳食宝塔的角色）：
- breakfast_main  早餐主食（麦满分 / 早安营养卷 / 炒双蛋堡 / 粥）
- burger_main     正餐主食（堡 / 汉堡 / 卷）
- protein_side    蛋白质小食（炸鸡 / 鸡翅 / 鸡排 / 香肠 / 鸡球）
- starch_side     薯类与谷物小食（薯条 / 薯饼 / 油条 / 玉米杯）
- veg_fruit       蔬果（苹果片等）
- dairy_dessert   乳制品与甜品（冰淇淋 / 新地 / 麦旋风 / 派 / 雪冰 / 拉明顿）
- drink           饮品
"""

from __future__ import annotations

import re
import unicodedata

# ------------------------------------------------------------------ 归一化工具
SIZE_TOKENS = ["迷你", "大杯", "中杯", "小杯", "特大杯", "大", "中", "小", "盒装", "瓶装"]

_FW_MAP = {
    "（": "(", "）": ")", "【": "[", "】": "]", "〔": "[", "〕": "]",
    "，": ",", "“": '"', "”": '"', "‘": "'", "’": "'",
    "－": "-", "—": "-", "～": "~", "！": "!", "？": "?",
}


def normalize(name: str) -> str:
    """统一全角/半角、去空格与常见装饰符号，用于跨数据源对账。"""
    s = unicodedata.normalize("NFKC", name or "")
    for k, v in _FW_MAP.items():
        s = s.replace(k, v)
    s = re.sub(r"\s+", "", s)
    s = s.replace('"', "").replace("'", "")
    return s.strip()


def strip_size(name: str) -> str:
    """剥掉规格词，用于把「可乐中杯」对齐到菜单里的「可乐」。"""
    s = normalize(name)
    for tok in SIZE_TOKENS:
        if s.endswith(tok) and len(s) > len(tok):
            s = s[: -len(tok)]
            break
        if s.startswith(tok) and len(s) > len(tok):
            s = s[len(tok):]
            break
    return s


# ------------------------------------------------------------------ 分类规则
COMBO_PATTERN = re.compile(
    r"(套餐|三件套|四件套|八件套|五件套|双件套|随心配|随心选|双人餐|分享餐|"
    r"全餐|乐园餐|双全盒|小食盘|单品精选|徽章|酱$)"
)

# 规则按顺序命中即返回： (分类, 关键词列表)
# 顺序很关键：主食必须先于小食判定，否则「麦辣鸡腿汉堡」会被「鸡腿」抢走。
RULES: list[tuple[str, list[str]]] = [
    ("breakfast_main", [
        "麦满分", "早安营养卷", "炒双蛋堡", "粥",
    ]),
    ("burger_main", [
        "汉堡", "堡", "卷", "巨无霸", "不素之霸", "麦香鸡", "麦香鱼",
        "牛气满满", "果然多肉",
    ]),
    ("protein_side", [
        "麦乐鸡", "鸡翅", "V翅", "鸡排", "脆汁鸡", "香肠", "鸡球", "鸡架",
        "肉骨鸡", "鸡块", "鸡腿", "绝代双翅", "蜜汁快乐", "鸡薯",
    ]),
    ("starch_side", [
        "薯条", "薯饼", "玉米杯", "油条", "扭扭薯", "薯格",
    ]),
    ("veg_fruit", [
        "苹果片", "蔬菜",
    ]),
    ("dairy_dessert", [
        "冰淇淋", "新地", "麦旋风", "派", "雪冰", "拉明顿", "阿芙佳朵",
        "奶冻", "爆珠", "芝士条", "圆筒",
    ]),
    ("drink", [
        "可乐", "雪碧", "咖啡", "美式", "奶铁", "卡布", "玛奇朵", "红茶", "绿茶",
        "牛奶", "燕麦奶", "豆浆", "苹果汁", "橙", "柠檬", "纯悦", "怡泉",
        "朱古力", "黑巧", "抹茶", "奶昔", "气泡",
    ]),
]

# 显式例外：规则命中不了或会命错的单品
OVERRIDES: dict[str, str] = {
    # 套餐化命名的单品，其实是主食
    "专“薯鱼”你": "burger_main",
    "“苹板”支撑Pro": "burger_main",
    # 无糖饮料归入饮品
    "纯悦": "drink",
}
_OVERRIDE_INDEX = {normalize(k): v for k, v in OVERRIDES.items()}

CATEGORY_LABEL = {
    "breakfast_main": "早餐主食",
    "burger_main": "正餐主食",
    "protein_side": "蛋白质小食",
    "starch_side": "薯类谷物",
    "veg_fruit": "蔬果",
    "dairy_dessert": "乳制品甜品",
    "drink": "饮品",
    "unknown": "未分类",
}


def classify(name: str) -> str:
    """把餐品名归到七大角色之一。"""
    n = normalize(name)
    if n in _OVERRIDE_INDEX:
        return _OVERRIDE_INDEX[n]
    for category, keywords in RULES:
        for kw in keywords:
            if normalize(kw) in n:
                return category
    return "unknown"


SIZE_HINT_PATTERN = re.compile(
    r"(迷你|特大杯|大杯|中杯|小杯|[大中小](?=杯|份|薯条|薯格|薯饼))|(\d+\s*块)"
)


def size_hint(name: str) -> str:
    """从营养表名称里提取规格提示，用于下单时选规格。"""
    m = SIZE_HINT_PATTERN.search(normalize(name))
    return m.group(0) if m else ""


def is_combo(name: str) -> bool:
    """是否为捆绑套餐（营养数据无法拆分，规划时默认排除）。"""
    n = normalize(name)
    return bool(COMBO_PATTERN.search(n))


# ------------------------------------------------------------------ 时段适配
# 每餐的角色槽位：主槽必选，其余可选（None 表示可以不点）
# 饮品设为可选：自带白开水永远是最优解，不该为了凑结构强塞含糖饮料
MEAL_SLOTS: dict[str, list[tuple[str, bool]]] = {
    #  (角色, 是否必选)
    "breakfast": [
        ("breakfast_main", True),
        ("protein_side", False),
        ("starch_side", False),
        ("drink", False),
        ("veg_fruit", False),
    ],
    "lunch": [
        ("burger_main", True),
        ("protein_side", False),
        ("starch_side", False),
        ("drink", False),
        ("veg_fruit", False),
    ],
    "dinner": [
        ("burger_main", True),
        ("protein_side", False),
        ("starch_side", False),
        ("drink", False),
        ("veg_fruit", False),
    ],
}

# 单餐能量允许落在目标值的这个倍率区间内（膳食指南的三餐供能比换算）
KCAL_BAND = (0.75, 1.25)

# 甜品作为「可选加餐」，默认不进正餐，除非用户明确要
DESSERT_CATEGORY = "dairy_dessert"


def slot_categories(meal: str) -> list[str]:
    return [c for c, _ in MEAL_SLOTS[meal]]

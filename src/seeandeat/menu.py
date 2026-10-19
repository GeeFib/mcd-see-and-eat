"""从麦当劳 MCP 拉取营养数据与在售菜单，合成「可规划的餐品池」。

数据源（全部来自麦当劳 MCP Server，无本地臆造）：
1. list-nutrition-foods —— 餐品营养成分（能量/蛋白质/脂肪/碳水/钠/钙）
2. query-nearby-stores / delivery-query-stores —— 门店
3. query-meals —— 门店当前可售卖餐品（含价格）

合成逻辑：以「在售餐品」为准（保证真能点到），用营养表补齐营养字段；
营养表里查不到的一律剔除，绝不猜数值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import taxonomy
from .mcp_client import McdMcpClient

NUTRI_FIELDS = ("kj", "kcal", "protein", "fat", "carb", "sodium", "calcium")


@dataclass
class FoodItem:
    """一个可点的单品 + 它的营养与价格。"""

    name: str
    category: str
    kcal: float
    protein: float
    fat: float
    carb: float
    sodium: float
    calcium: float
    kj: float = 0.0
    price: float | None = None
    code: str | None = None           # 菜单编码，下单用
    orig_price: float | None = None
    image: str | None = None
    size: str = ""                    # 规格提示（中杯 / 5块 …），下单选规格用
    menu_name: str = ""              # 菜单上的售卖名（可能比营养名更泛化）
    nutrition_source: str = "mcp"     # mcp | snapshot
    tags: list[str] = field(default_factory=list)

    def macro_ratios(self) -> tuple[float, float, float]:
        """蛋白质/脂肪/碳水 供能比（kcal 为 0 时返回全 0）。"""
        if self.kcal <= 0:
            return (0.0, 0.0, 0.0)
        p, f, c = self.protein * 4, self.fat * 9, self.carb * 4
        tot = p + f + c or 1
        return (p / tot, f / tot, c / tot)

    @property
    def category_label(self) -> str:
        return taxonomy.CATEGORY_LABEL.get(self.category, "未分类")


# ------------------------------------------------------------------ 解析
def parse_nutrition(raw: Any) -> list[dict[str, Any]]:
    """解析 list-nutrition-foods 返回。

    返回体是 `[N]{...}:\\n  名称,null,kj,kcal,蛋白,脂肪,碳水,钠,钙` 的紧凑文本。
    """
    if isinstance(raw, dict):
        raw = raw.get("data", raw)
    if isinstance(raw, str):
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    elif isinstance(raw, list):
        lines = []
        for row in raw:
            if isinstance(row, dict):
                lines.append(row)
        if lines:
            return lines
        lines = [str(x).strip() for x in raw if str(x).strip()]
    else:
        raise ValueError(f"无法识别的营养数据格式：{type(raw)}")

    items: list[dict[str, Any]] = []
    for ln in lines:
        if isinstance(ln, dict):
            items.append(ln)
            continue
        if ln.startswith("[") or "{" in ln and "productName" in ln:
            continue  # 表头
        parts = ln.split(",")
        if len(parts) < 9:
            continue
        def num(v: str) -> float:
            v = v.strip()
            return 0.0 if v in ("null", "", "-", "NULL") else float(v)
        items.append(
            {
                "name": parts[0].strip(),
                "kj": num(parts[2]),
                "kcal": num(parts[3]),
                "protein": num(parts[4]),
                "fat": num(parts[5]),
                "carb": num(parts[6]),
                "sodium": num(parts[7]),
                "calcium": num(parts[8]),
            }
        )
    return items


def parse_menu(raw: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """解析 query-meals 返回，得到 [{code, name, price, ...}] 与 {code: 分类名}。"""
    if isinstance(raw, str):
        raw = json.loads(raw)
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    meals = data.get("meals", {}) if isinstance(data, dict) else {}
    categories = data.get("categories", []) if isinstance(data, dict) else []

    cat_of: dict[str, str] = {}
    for cat in categories:
        cname = cat.get("name", "").replace("\n", "")
        for m in cat.get("meals", []) or []:
            if isinstance(m, dict) and m.get("code"):
                cat_of[str(m["code"])] = cname
            elif isinstance(m, str):
                cat_of[m] = cname

    out: list[dict[str, Any]] = []
    for code, m in meals.items():
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "code": str(code),
                "name": m.get("name", ""),
                "price": _to_float(m.get("currentPrice")),
                "orig_price": _to_float(m.get("originalPrice")),
                "image": m.get("image"),
                "menu_category": cat_of.get(str(code), ""),
                "tags": [c.get("name", "") for c in (m.get("tags") or []) if isinstance(c, dict)]
                or (m.get("tags") or []),
            }
        )
    return out, cat_of


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 匹配
def _menu_index(menu_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{归一化名: 菜单行}，同名保留有价格的那条。"""
    idx: dict[str, dict[str, Any]] = {}
    for m in menu_items:
        key = taxonomy.normalize(m.get("name", ""))
        if not key:
            continue
        if key not in idx or (idx[key].get("price") is None and m.get("price") is not None):
            idx[key] = m
    return idx


def match_menu_item(
    nut_name: str, menu_idx: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """把营养表条目对上在售菜单。

    营养表带精确规格（可乐中杯 / 麦乐鸡5块），菜单里通常是通用名（可乐 / 麦乐鸡），
    所以以营养表为主单位去反查菜单：精确 → 去规格 → 双向子串（取最长命中）。
    """
    n = taxonomy.normalize(nut_name)
    if not n:
        return None
    if n in menu_idx:
        return menu_idx[n]
    base = taxonomy.strip_size(nut_name)
    if base in menu_idx:
        return menu_idx[base]
    best: tuple[int, dict[str, Any]] | None = None
    for key, row in menu_idx.items():
        if len(key) < 2:
            continue
        if key in n or n in key:
            score = len(key) if key in n else len(n)
            if best is None or score > best[0]:
                best = (score, row)
    return best[1] if best else None


# ------------------------------------------------------------------ 组装
def build_pool(
    nut_items: list[dict[str, Any]],
    menu_items: list[dict[str, Any]],
) -> tuple[list[FoodItem], list[str]]:
    """合成餐品池。

    规划单位 = 营养表条目（带精确规格，营养值最准），
    价格与下单编码 = 反查到的在售菜单行（保证真的点得到）。
    双向对齐不上的一律剔除，绝不用估算值凑数。
    """
    menu_idx = _menu_index(menu_items)
    matched_codes: set[str] = set()
    pool: list[FoodItem] = []
    dropped: list[str] = []

    for row in nut_items:
        nut_name = (row.get("name") or row.get("productName") or "").strip()
        if not nut_name:
            continue
        m = match_menu_item(nut_name, menu_idx)
        if m is None:
            continue
        menu_name = m.get("name", "").strip()
        if taxonomy.is_combo(menu_name):
            continue
        pool.append(
            FoodItem(
                name=nut_name,
                category=taxonomy.classify(nut_name),
                kcal=float(row.get("kcal", 0) or 0),
                protein=float(row.get("protein", 0) or 0),
                fat=float(row.get("fat", 0) or 0),
                carb=float(row.get("carb", 0) or 0),
                sodium=float(row.get("sodium", 0) or 0),
                calcium=float(row.get("calcium", 0) or 0),
                kj=float(row.get("kj", 0) or 0),
                price=m.get("price"),
                orig_price=m.get("orig_price"),
                code=m.get("code"),
                image=m.get("image"),
                size=taxonomy.size_hint(nut_name),
                menu_name=menu_name,
                tags=list(m.get("tags") or []),
            )
        )
        if m.get("code"):
            matched_codes.add(str(m["code"]))

    # 去重：同名营养条目只留一条
    seen: dict[str, FoodItem] = {}
    for item in pool:
        seen.setdefault(taxonomy.normalize(item.name), item)

    # 在售但营养表没覆盖的单品，明确告知（多为限定新品）
    for m in menu_items:
        name = m.get("name", "").strip()
        if not name or str(m.get("code", "")) in matched_codes:
            continue
        if taxonomy.is_combo(name):
            dropped.append(f"{name} —— 捆绑套餐，营养无法拆分，不参与营养搭配")
        else:
            dropped.append(f"{name} —— 麦当劳营养表暂无对应数据，为保证准确性已剔除")

    return list(seen.values()), dropped


# ------------------------------------------------------------------ 取数入口
def nutrition_from_mcp(client: McdMcpClient) -> list[dict[str, Any]]:
    return parse_nutrition(client.tool("list-nutrition-foods"))


def nutrition_from_snapshot(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    return parse_nutrition(raw)


def menu_from_mcp(
    client: McdMcpClient,
    store_code: str,
    order_type: int = 1,
    be_type: int = 1,
    be_code: str | None = None,
) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
    if be_code:
        args["beCode"] = be_code
    items, _ = parse_menu(client.tool("query-meals", args))
    return items


def menu_from_snapshot(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "menus" in raw:
        # 新格式：{slot: [items]}，这里做一份全量并集
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for items in raw["menus"].values():
            for it in items:
                key = str(it.get("code"))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(it)
        return merged
    items, _ = parse_menu(raw)
    return items


def slot_menus_from_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    """读分时快照；老格式（单菜单）则三餐共用。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "menus" in raw:
        return {k: list(v) for k, v in raw["menus"].items()}
    items, _ = parse_menu(raw)
    return {slot: list(items) for slot in ("breakfast", "lunch", "dinner")}


def pick_store(client: McdMcpClient, keyword: str | None = None, be_type: int = 1) -> dict[str, Any]:
    """选一家门店：默认取收藏门店里的第一家。"""
    args: dict[str, Any] = {"beType": be_type, "searchType": 1}
    if keyword:
        args.update({"searchType": 2, "keyword": keyword})
    raw = client.tool("query-nearby-stores", args)
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    stores = data if isinstance(data, list) else data.get("data", [])
    if not stores:
        raise RuntimeError("没有查询到可用门店，请先确认账号有收藏餐厅或提供城市关键词")
    return stores[0]

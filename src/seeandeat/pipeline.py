"""端到端流水线：门店 → 分时菜单 → 营养目标 → 搭配方案。

关键点：麦当劳分时段供应（早餐约 05:00-10:30，之后切正餐菜单），
所以要按「餐次时段」分别调用 query-meals 的 reservationDate，
否则早餐只能拿到麦乐鸡和汉堡，配不出合理的早餐。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import menu as menu_mod
from . import planner
from .dri import Profile, compute_target, DailyTarget
from .mcp_client import McdMcpClient
from .menu import FoodItem

# 各餐次的取餐时间点（用于 reservationDate）
SLOT_TIME = {"breakfast": (8, 30), "lunch": (12, 0), "dinner": (18, 30)}
SLOT_ORDER = ("breakfast", "lunch", "dinner")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass
class PlanContext:
    profile: Profile
    target: DailyTarget
    store: dict[str, Any]
    pools: dict[str, list[FoodItem]]
    plans: list[planner.Plan]
    dropped: list[str]
    slot_dates: dict[str, str]
    live: bool = True
    warnings: list[str] = field(default_factory=list)


def _slot_datetime(now: datetime, slot: str) -> datetime:
    """取该餐次最近一次可用的时间点：今天还没过就用今天，否则顺延到明天。"""
    h, m = SLOT_TIME[slot]
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def current_time(client: McdMcpClient | None) -> datetime:
    """优先用 MCP 的 now-time-info 拿服务器时间，失败则回退本地时间。"""
    if client is not None:
        try:
            raw = client.tool("now-time-info")
            data = raw.get("data", raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                for key in ("datetime", "time", "currentTime", "now"):
                    v = data.get(key)
                    if isinstance(v, str):
                        return datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")
                ts = data.get("timestamp") or data.get("timeMillis")
                if isinstance(ts, (int, float)):
                    ms = ts / 1000 if ts > 1e11 else ts
                    return datetime.fromtimestamp(ms)
        except Exception:  # 时间拿不到不影响主流程
            pass
    return datetime.now()


def resolve_store(client: McdMcpClient, store_code: str | None, keyword: str | None) -> dict[str, Any]:
    if store_code:
        for src in ("query-nearby-stores",):
            try:
                raw = client.tool(src, {"beType": 1, "searchType": 1})
                data = raw.get("data", raw) if isinstance(raw, dict) else raw
                stores = data if isinstance(data, list) else (data.get("data") or [])
                for s in stores:
                    if str(s.get("storeCode")) == str(store_code):
                        return s
            except Exception:
                continue
        return {"storeCode": store_code, "storeName": f"门店 {store_code}"}
    return menu_mod.pick_store(client, keyword)


def fetch_slot_menus(
    client: McdMcpClient,
    store: dict[str, Any],
    now: datetime,
    order_type: int = 1,
    be_type: int = 1,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """按餐次分别拉菜单。"""
    store_code = str(store.get("storeCode"))
    be_code = store.get("beCode") or None
    out: dict[str, list[dict[str, Any]]] = {}
    dates: dict[str, str] = {}
    for slot in SLOT_ORDER:
        when = _slot_datetime(now, slot)
        dates[slot] = when.strftime("%Y-%m-%d %H:%M")
        args: dict[str, Any] = {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "reservationDate": dates[slot],
        }
        if be_code:
            args["beCode"] = be_code
        try:
            items, _ = menu_mod.parse_menu(client.tool("query-meals", args))
        except Exception as exc:
            out[slot] = []
            dates[slot] = f"{dates[slot]}（拉取失败：{exc}）"
            continue
        out[slot] = items
    return out, dates


def run(
    profile: Profile,
    *,
    store_code: str | None = None,
    keyword: str | None = None,
    options: planner.PlanOptions | None = None,
    offline: bool = False,
) -> PlanContext:
    """主流程。offline=True 时用仓库自带的快照跑（无网络也能演示）。"""
    options = options or planner.PlanOptions()
    target = compute_target(profile)

    if offline:
        nut = menu_mod.nutrition_from_snapshot(DATA_DIR / "nutrition_snapshot.json")
        snap = json.loads((DATA_DIR / "menu_snapshot.json").read_text(encoding="utf-8"))
        menus = menu_mod.slot_menus_from_snapshot(DATA_DIR / "menu_snapshot.json")
        menus = {s: menus.get(s, []) for s in SLOT_ORDER}
        store = {
            "storeCode": snap.get("storeCode", "1420621"),
            "storeName": f"{snap.get('storeName', '麦当劳')}（快照 {snap.get('capturedAt', '')}）",
        }
        live = False
        now = datetime.now()
        dates = snap.get("slotDates") or {s: "快照数据" for s in SLOT_ORDER}
    else:
        client = McdMcpClient()
        now = current_time(client)
        store = resolve_store(client, store_code, keyword)
        nut = menu_mod.nutrition_from_mcp(client)
        menus, dates = fetch_slot_menus(client, store, now)
        live = True

    pools: dict[str, list[FoodItem]] = {}
    dropped: list[str] = []
    seen_drop: set[str] = set()
    for slot in SLOT_ORDER:
        pool, drop = menu_mod.build_pool(nut, menus.get(slot, []))
        pools[slot] = pool
        for d in drop:
            if d not in seen_drop:
                seen_drop.add(d)
                dropped.append(d)

    warnings: list[str] = []
    for slot in SLOT_ORDER:
        if not pools[slot]:
            warnings.append(f"{slot} 菜单为空，该餐次无法规划")

    plans = planner.make_plans(pools, target, options)
    if not plans:
        warnings.append("当前门店分时菜单与营养表的交集为空，无法生成方案，请换门店或稍后再试")

    return PlanContext(
        profile=profile,
        target=target,
        store=store,
        pools=pools,
        plans=plans,
        dropped=dropped,
        slot_dates=dates,
        live=live,
        warnings=warnings,
    )

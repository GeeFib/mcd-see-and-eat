"""「换一批」需要的批次记忆。

换一批的本质是：「上一批点过的，这一批别再来一遍」。
所以要把每次交付的餐品记下来，下一批跑的时候当作回避清单。

存到 ~/.mcd-see-and-eat/batches.json（可用 SEEANDEAT_STATE_DIR 覆盖），
默认保留最近 3 批；不写进仓库，也不含任何 Token。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

KEEP_BATCHES = 4


def state_dir() -> Path:
    d = Path(os.environ.get("SEEANDEAT_STATE_DIR") or (Path.home() / ".mcd-see-and-eat"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path() -> Path:
    return state_dir() / "batches.json"


def load() -> dict[str, Any]:
    p = state_path()
    if not p.exists():
        return {"batches": []}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"batches": []}
    if not isinstance(doc, dict) or not isinstance(doc.get("batches"), list):
        return {"batches": []}
    return doc


def save(doc: dict[str, Any]) -> None:
    doc["batches"] = (doc.get("batches") or [])[-KEEP_BATCHES:]
    state_path().write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ 记录一批
def record(
    *,
    store_code: str,
    slots: tuple[str, ...],
    plans: list[Any],
    profile_key: str = "",
    coupons: list[str] | None = None,
    reroll: int = 0,
) -> dict[str, Any]:
    """记录本次交付。

    语义：不加 --reroll 就是「第一批」——同一个门店 + 同一个用户档案
    重新跑一次意味着重新开始，历史清空；--reroll 才是接着上一批往下换。
    """
    shop: dict[str, list[str]] = {}
    mains: list[str] = []
    mains_by_slot: dict[str, list[str]] = {}
    for p in plans:
        for slot in getattr(p, "bundles", {}):
            b = p.bundles[slot]
            names = [it.name for it in b.items]
            shop.setdefault(slot, [])
            for n in names:
                if n not in shop[slot]:
                    shop[slot].append(n)
            if names and names[0] not in mains:
                mains.append(names[0])
            if names:
                mains_by_slot.setdefault(slot, [])
                if names[0] not in mains_by_slot[slot]:
                    mains_by_slot[slot].append(names[0])
    doc = load()
    batches = doc.get("batches") or []
    if reroll:
        # 接着换：保留同范围的历史批次（soft 回避要用）
        kept = batches
        last_round = 0
        for b in kept:
            if _same_scope(b, store_code, profile_key):
                last_round = max(last_round, int(b.get("round") or 0))
        nxt = last_round + 1
    else:
        # 首次/重跑：同门店同档案重新开始
        kept = [b for b in batches if not _same_scope(b, store_code, profile_key)]
        nxt = 1
    entry = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "storeCode": store_code,
        "profileKey": profile_key,
        "slots": list(slots),
        "mains": mains,
        "mainsBySlot": mains_by_slot,
        "items": shop,
        "plans": [getattr(p, "preset_label", "") for p in plans],
        "coupons": coupons or [],
        "round": nxt,
    }
    doc["batches"] = kept + [entry]
    save(doc)
    return entry


def _same_scope(batch: dict[str, Any], store_code: str, profile_key: str) -> bool:
    if store_code and batch.get("storeCode") and batch["storeCode"] != store_code:
        return False
    if profile_key and batch.get("profileKey") and batch["profileKey"] != profile_key:
        return False
    return True


def recall(
    store_code: str = "",
    profile_key: str = "",
) -> tuple[list[str], list[str], int]:
    """返回 (必换主食清单, 尽量回避清单, 下一轮序号)。

    只认「同门店 + 同用户档案」的历史，换门店或换人就当第一批。
    """
    doc = load()
    scoped = [b for b in (doc.get("batches") or []) if _same_scope(b, store_code, profile_key)]
    if not scoped:
        return [], [], 1

    # 必换：最近两批的主食都换掉，保证连着换两三次不会绕回同一套
    hard: list[str] = []
    for b in scoped[-2:]:
        for n in (b.get("mains") or []):
            if n not in hard:
                hard.append(n)
    # 尽量换：所有批次的餐品
    soft: list[str] = []
    for b in scoped:
        for names in (b.get("items") or {}).values():
            for n in names:
                if n not in soft:
                    soft.append(n)
    rounds = max((int(b.get("round") or 0) for b in scoped), default=0)
    return hard, soft, rounds + 1


def previous(store_code: str = "", profile_key: str = "") -> dict[str, Any] | None:
    """同门店同档案的上一批记录。

    用途：告诉用户「这一批到底换掉了什么」——比单说「换一批了」有用得多。
    """
    doc = load()
    scoped = [b for b in (doc.get("batches") or []) if _same_scope(b, store_code, profile_key)]
    return scoped[-1] if scoped else None


def clear() -> None:
    p = state_path()
    if p.exists():
        p.unlink()

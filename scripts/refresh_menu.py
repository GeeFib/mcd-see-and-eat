#!/usr/bin/env python3
"""从麦当劳 MCP 拉取最新营养表、分时菜单与门店优惠券，写入 data/ 快照。

用法：
    export MCD_MCP_TOKEN=<你的 Token>
    python scripts/refresh_menu.py [--store-code 1420621] [--keyword 蛇口] [--no-coupons]

快照用途：离线演示 / 单元测试。正式规划请用 scripts/plan.py 走实时接口。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seeandeat import menu as menu_mod, pipeline  # noqa: E402
from seeandeat.mcp_client import McdMcpClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store-code", default=None, help="门店编码，不填取收藏门店第一家")
    ap.add_argument("--keyword", default=None, help="按关键词/位置搜门店")
    ap.add_argument("--out-dir", default=str(ROOT / "data"))
    ap.add_argument("--no-coupons", action="store_true", help="不拉优惠券快照")
    args = ap.parse_args()

    client = McdMcpClient()
    now = pipeline.current_time(client)
    store = pipeline.resolve_store(client, args.store_code, args.keyword)
    print(f"门店：{store.get('storeName')}（{store.get('storeCode')}）")

    nutrition = menu_mod.nutrition_from_mcp(client)
    (Path(args.out_dir) / "nutrition_snapshot.json").write_text(
        json.dumps(nutrition, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"营养条目：{len(nutrition)}")

    menus, dates = pipeline.fetch_slot_menus(client, store, now)
    for slot, items in menus.items():
        print(f"{slot}（{dates[slot]}）：{len(items)} 个在售餐品")

    payload = {
        "capturedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "storeCode": str(store.get("storeCode")),
        "storeName": store.get("storeName"),
        "slotDates": dates,
        "menus": menus,
    }
    (Path(args.out_dir) / "menu_snapshot.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"已写入 {args.out_dir}/menu_snapshot.json")

    if not args.no_coupons:
        coupons = pipeline.save_coupon_snapshot(
            client, store, Path(args.out_dir) / "coupons_snapshot.json"
        )
        print(f"优惠券 {len(coupons)} 张（含券后价）已写入 {args.out_dir}/coupons_snapshot.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

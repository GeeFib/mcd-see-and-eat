#!/usr/bin/env python3
"""优惠券工具：看有哪些券、哪些能对上餐品、券后价多少，以及在需要时一键领券。

用法：
    export MCD_MCP_TOKEN=<你的 Token>

    # 1) 看门店可用券 + 麦麦省可领券
    python scripts/coupons.py --store-code 1420621

    # 2) 看券后价（只用算价接口试算，不下单）
    python scripts/coupons.py --store-code 1420621 --quote

    # 3) 一键领取麦麦省当前可领的券（会改变你的账户，需你确认后执行）
    python scripts/coupons.py --claim

说明：领券是账号级动作，本脚本只在显式传 --claim 时才调用 auto-bind-coupons；
     计算券后价会调用 calculate-price，不会产生订单。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seeandeat import coupon as coupon_mod, menu as menu_mod, pipeline  # noqa: E402
from seeandeat.mcp_client import McdMcpClient, McpError  # noqa: E402


def money(v: float | None) -> str:
    return f"¥{v:.1f}" if v else "—"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="麦当劳优惠券体检：有哪些、能不能用、券后多少钱")
    ap.add_argument("--store-code", default=None, help="门店编码，不填取收藏门店第一家")
    ap.add_argument("--keyword", default=None, help="按关键词/位置搜门店")
    ap.add_argument("--quote", action="store_true", help="用算价接口试算券后价")
    ap.add_argument("--claim", action="store_true", help="一键领取麦麦省当前可领的券")
    ap.add_argument("--refresh-snapshot", action="store_true",
                    help="把门店可用券（含券后价）写入 data/coupons_snapshot.json 供离线演示")
    args = ap.parse_args(argv)

    client = McdMcpClient()
    store = pipeline.resolve_store(client, args.store_code, args.keyword)
    code = str(store.get("storeCode") or "")
    print(f"门店：{store.get('storeName')}（{code}）\n")

    # 麦麦省里还没领的
    claimable = coupon_mod.fetch_claimable(client)
    todo = [c for c in claimable if not c.claimed]
    if claimable:
        print(f"麦麦省券 {len(claimable)} 张，其中 {len(todo)} 张待领取：")
        for c in todo[:15]:
            print(f"  + {c.title}")
        if args.claim and todo:
            print("\n正在一键领取…")
            print(f"  {coupon_mod.claim_all(client)}")
            print("（领完可重新运行本命令查看门店可用券）")
        elif todo:
            print("  → 想领：加 --claim（会写入你的麦当劳账号）")
        print()

    coupons = coupon_mod.fetch_coupons(client, code, be_code=store.get("beCode") or None)
    if not coupons:
        print("当前门店没有任何可用券。")
        return 0

    print(f"当前门店可用券 {len(coupons)} 张：")
    for c in coupons:
        print(f"  - {c.title}　[{c.kind_label}]　有效期 {c.trade_datetime}")
        for p in c.products:
            print(f"      券商品 {p.code}｜{p.name}")
    print()

    # 能不能对上营养表里的餐品（决定能不能进搭配）
    nut = menu_mod.nutrition_from_mcp(client)
    menus, _ = pipeline.fetch_slot_menus(client, store, pipeline.current_time(client))
    pool = []
    for items in menus.values():
        p, _drop = menu_mod.build_pool(nut, items)
        pool.extend(p)
    coupon_mod.match_pool(coupons, pool)
    usable = [c for c in coupons if c.matched_items]
    if usable:
        print(f"其中 {len(usable)} 张能对上可搭配的餐品：")
        for c in usable:
            print(f"  - {c.title} → 可用于：{'、'.join(c.matched_items)}")
    else:
        print("这些券都对应不上「有营养数据的在售单品」（多为套餐券），无法进入营养搭配。")
    print()

    if args.quote or args.refresh_snapshot:
        target = usable or coupons
        coupon_mod.enrich_quotes(client, target, code,
                                 be_code=store.get("beCode") or None,
                                 only_matched=False, limit=20)
        print("券后价试算（calculate-price 实测，单位：元）：")
        for c in target:
            if c.quote:
                mode = "同价加量" if c.kind in ("bogo", "half") else "现金减免"
                print(f"  - {c.title}　[{mode}]　"
                      f"内容原价 {money(c.quote.origin)} → 实付 {money(c.quote.price)}"
                      f"　差额 {money(c.quote.discount)}")
            else:
                print(f"  - {c.title}　（当前不可核销或算价失败）")
        print()

    if args.refresh_snapshot:
        out = pipeline.save_coupon_snapshot(client, store)
        print(f"已写入 data/coupons_snapshot.json（{len(out)} 张券，含券后价）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except McpError as exc:
        print(f"[MCP 错误] {exc}", file=sys.stderr)
        raise SystemExit(1)

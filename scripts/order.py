#!/usr/bin/env python3
"""把 plan.py 产出的方案真正下单。

流程：读方案 JSON → 查门店可用券 → calculate-price 算价 →（需 --confirm）create-order 下单。

⚠️ create-order 会产生真实订单，必须显式加 --confirm 才会执行。

用法：
    python scripts/plan.py ... --json plan.json
    # 先看看价格和可用券，不下单
    python scripts/order.py --plan plan.json --meal lunch --preset balanced
    # 用上方案里推荐的券（券商品 code + couponId + couponCode 三件套）
    python scripts/order.py --plan plan.json --meal lunch --preset balanced --use-coupon
    # 确认后再真下单
    python scripts/order.py --plan plan.json --meal lunch --preset balanced --use-coupon --confirm

优惠券的核销要点（已实测）：
麦当劳 MCP 要求 items[] 传「券商品自己的 productCode」，并同时带
couponId + couponCode；plan.json 里每条 couponHit 都带了这三个值，
--use-coupon 会自动把对应餐品替换成券商品，并用真实算价结果核对减免额。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seeandeat.mcp_client import McdMcpClient, McpError  # noqa: E402


def _data(raw):
    if isinstance(raw, dict):
        return raw.get("data", raw)
    return raw


def _coupon_hits(plan: dict, slots: list[str]) -> list[dict]:
    return [h for h in (plan.get("couponHits") or []) if h.get("slot") in slots]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把搭配方案下单到麦当劳")
    ap.add_argument("--plan", required=True, help="plan.py --json 产出的文件")
    ap.add_argument("--meal", default="lunch", help="breakfast / lunch / dinner / all")
    ap.add_argument("--preset", default=None, help="方案名，默认取第一套")
    ap.add_argument("--store-code", default=None, help="覆盖门店编码")
    ap.add_argument("--use-coupon", action="store_true",
                    help="核销方案里推荐的优惠券（换成券商品编码并带 couponId/couponCode）")
    ap.add_argument("--take-way", default=None, help="取餐方式 code，来自 calculate-price 的 takeWayList")
    ap.add_argument("--order-type", type=int, default=1, help="1-到店 2-外送")
    ap.add_argument("--be-type", type=int, default=1, help="1-到店取餐 2-麦乐送 5-得来速 6-团餐")
    ap.add_argument("--reservation-date", default=None, help="预约时间 yyyy-MM-dd HH:mm")
    ap.add_argument("--tableware", action="store_true", help="需要餐具")
    ap.add_argument("--confirm", action="store_true", help="真正创建订单（会扣款）")
    args = ap.parse_args(argv)

    doc = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    plans = doc.get("plans") or []
    if not plans:
        print("方案文件里没有方案", file=sys.stderr)
        return 1
    plan = next((p for p in plans if p.get("preset") == args.preset), plans[0])

    slots = list(plan["meals"].keys())
    if args.meal != "all":
        if args.meal not in slots:
            print(f"方案里没有 {args.meal}，可选：{slots}", file=sys.stderr)
            return 2
        slots = [args.meal]

    hits = _coupon_hits(plan, slots)
    used_coupons: list[dict] = []
    items: list[dict] = []
    skipped_hits: list[dict] = []

    for slot in slots:
        for it in plan["meals"][slot]:
            if not it.get("code"):
                print(f"[跳过] {it['name']} 没有菜单编码", file=sys.stderr)
                continue
            items.append({"productCode": str(it["code"]), "quantity": 1})

    if args.use_coupon and hits:
        for h in hits:
            code = str(h.get("redeemProductCode") or "")
            if not code or not h.get("couponId"):
                skipped_hits.append(h)
                continue
            # 把被券覆盖的那份餐品替换成券商品（券商品自带「买一送一」等权益）
            items = [x for x in items if x["productCode"] != code]
            # 券商品与餐品不是同一 code 时，去掉一份同名餐品，避免重复下单
            same_name_codes = {str(it["code"]) for it in plan["meals"][h["slot"]]
                               if it["name"] == h.get("item")}
            for c in same_name_codes:
                items = [x for x in items if x["productCode"] != c]
            entry = {"productCode": code, "quantity": 1,
                     "couponId": h.get("couponId", ""), "couponCode": h.get("couponCode", "")}
            if entry not in items:
                items.append(entry)
            used_coupons.append(h)

    if not items:
        print("没有可下单的餐品", file=sys.stderr)
        return 1

    store_code = args.store_code or str(doc.get("store", {}).get("storeCode", ""))
    if not store_code:
        print("缺少门店编码，请用 --store-code 指定", file=sys.stderr)
        return 2

    client = McdMcpClient()
    common: dict = {
        "storeCode": store_code,
        "orderType": args.order_type,
        "beType": args.be_type,
    }
    if args.reservation_date:
        common["reservationDate"] = args.reservation_date

    print(f"门店 {store_code}　方案「{plan.get('presetLabel')}」　餐次 {'+'.join(slots)}")
    if used_coupons:
        print(f"已带上 {len(used_coupons)} 张券：")
        for h in used_coupons:
            print(f"  - {h.get('couponTitle')}（{h.get('kindLabel')}）"
                  f"　作用在 {h.get('item')}　预计省 ¥{h.get('saving', 0):.1f}")
            if h.get("note"):
                print(f"      {h['note']}")
    elif hits:
        print(f"方案里这餐有 {len(hits)} 处可用券，但未加 --use-coupon，按原价计算：")
        for h in hits:
            print(f"  - {h.get('couponTitle')} → {h.get('item')}（可省 ¥{h.get('saving', 0):.1f}）")
    for h in skipped_hits:
        print(f"[跳过券] {h.get('couponTitle')} 缺少核销参数", file=sys.stderr)
    for it in items:
        extra = "（券商品）" if it.get("couponId") else ""
        print(f"  - {it['productCode']} × {it['quantity']}{extra}")

    # 1) 可用优惠券
    try:
        coupons = _data(client.tool("query-store-coupons", dict(common)))
        lst = coupons if isinstance(coupons, list) else (coupons or {}).get("list") or []
        if lst:
            print(f"\n可用优惠券 {len(lst)} 张：")
            for c in lst[:8]:
                title = c.get("title") or c.get("name") or c.get("couponName") or "未命名券"
                goods = "、".join(
                    p.get("productName", "") for p in (c.get("products") or [])
                )
                print(f"  - {title}　有效期 {c.get('tradeDateTime', '-')}"
                      + (f"　适用：{goods}" if goods else ""))
        else:
            print("\n当前门店没有可用优惠券")
    except McpError as exc:
        print(f"\n[查券失败] {exc}", file=sys.stderr)

    # 2) 算价
    price_payload = dict(common)
    price_payload["items"] = items
    price_payload["needTableware"] = bool(args.tableware)
    result = _data(client.tool("calculate-price", price_payload))
    money = lambda v: f"¥{(v or 0) / 100:.2f}"  # noqa: E731
    print(
        f"\n商品 {money(result.get('productPrice'))}　"
        f"打包 {money(result.get('packingPrice'))}　"
        f"配送 {money(result.get('deliveryPrice'))}　"
        f"优惠 -{money(result.get('discount'))}　"
        f"应付 {money(result.get('price'))}"
    )
    for p in result.get("productList", []) or []:
        print(f"  - {p.get('productName')} × {p.get('quantity')}　{money(p.get('subtotal'))}")
    ways = result.get("takeWayList") or []
    if ways:
        print("取餐方式：" + "、".join(f"{w.get('code')}({w.get('title')})" for w in ways))

    print("\n提示：当前 MCP 的 calculate-price 对多规格餐品按默认规格计价，"
          "杯型/份量如需调整，请在麦当劳 App 确认。")

    if not args.confirm:
        print("\n未加 --confirm，到此为止，未下单。")
        return 0

    if args.order_type == 1 and not args.take_way:
        if ways:
            args.take_way = ways[0].get("code")
        else:
            print("到店场景必须指定 --take-way", file=sys.stderr)
            return 2

    order_payload = dict(common)
    order_payload["items"] = items
    order_payload["needTableware"] = bool(args.tableware)
    if args.take_way:
        order_payload["takeWayCode"] = args.take_way
    order = _data(client.tool("create-order", order_payload))
    print("\n下单成功：")
    print(json.dumps(order, ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except McpError as exc:
        print(f"[MCP 错误] {exc}", file=sys.stderr)
        raise SystemExit(1)

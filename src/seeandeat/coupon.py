"""优惠券：把「门店当前真能用的券」接到搭配方案上。

用真实 MCP 验证过的三个事实（这决定了实现方式）：

1. `query-store-coupons` 返回的 `data[]` 里每张券都带自己的**券商品** `productCode`
   （如「中薯条买一送一」= 9900012387）。
2. 想核销，`calculate-price` 的 `items[]` 必须传**券商品的 code**，
   并同时带上 `couponId` 与 `couponCode` 三件套；随便传个普通商品 code 会返回空。
3. 核销后返回的 `discount` 是真实减免额：
   「中薯条买一送一」商品价 27.0、优惠 13.5、应付 13.5 —— 等于花一份钱拿两份。

因此这里的「省钱」不是估算，是让麦当劳自己的算价引擎算出来的。
券只在用户本地账户生效，本模块不领取、不核销，只做查询与试算。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import taxonomy
from .mcp_client import McdMcpClient, McpError
from .menu import FoodItem

# ------------------------------------------------------------------ 券名解析
# 券名里的营销后缀，剥掉后剩下的才是「券作用在哪个餐品上」
_COUPON_SUFFIX = (
    "买一送一", "买1送1", "1送1", "买二送一", "第二份半价", "第二份0元",
    "第二件半价", "第二件0元", "半价", "5折", "6折", "7折", "8折", "免费",
    "1元", "2元", "9.9元", "特价", "立减", "优惠券", "兑换券", "券",
)
# 一张券对多份的语义
_BOGO_WORDS = ("买一送一", "买1送1", "1送1", "买二送一", "第二份0元", "第二件0元")
_HALF_WORDS = ("第二份半价", "第二件半价", "半价")

# 满减型（对整单生效，无法定位到单个餐品）
_THRESHOLD_RE = re.compile(r"满\s*\d+\s*(减|元减)")

# 品牌/营销词，比较餐品名时先剥掉（麦咖啡™冰美式中杯 ↔ 中杯冰美式）
_BRAND_WORDS = (
    "麦咖啡", "麦乐", "麦旋风", "优品", "阳光", "可口", "怡泉", "纯悦",
    "美汁源", "™", "TM", "【", "】", "（", "）", "(", ")",
)
# 这些词一旦出现在餐品名里，券名里也必须出现，否则算误配
# （「可乐」券不能匹配「无糖可口可乐」，「冰美式」券不能匹配「热美式中杯」）
_DIFF_WORDS = (
    "无糖", "0度", "零度", "冰", "热", "双层", "板烧", "香辣", "原味",
)

# 上面是「餐品有名、券名没写」→ 拦；这几个词反过来也要拦：
# 券是「双层深海鳕鱼堡」、餐品是「深海鳕鱼堡」，两个 SKU 营养和份量都不同。
# （「原味/板烧」不在此列——那是命名变体，麦记同一款常省略前缀）
_STRICT_DIFF = ("双层", "无糖", "0度", "零度")


# 只能剥「整词」的规格，不能剥裸的 大/中/小 ——
# 「中薯条 / 小薯条 / 大薯条」是三个不同 SKU，剥了就串味
_SIZE_LONG = ("特大杯", "迷你", "大杯", "中杯", "小杯", "盒装", "瓶装")


def _loose(name: str) -> str:
    """剥掉整词规格与品牌词，只留「这是什么」。（中杯冰美式 → 冰美式）"""
    s = taxonomy.normalize(strip_coupon_words(name) or name)
    for tok in _SIZE_LONG:
        if len(s) > len(tok) and s.endswith(tok):
            s = s[: -len(tok)]
            break
        if len(s) > len(tok) and s.startswith(tok):
            s = s[len(tok):]
            break
    for w in _BRAND_WORDS:
        s = s.replace(w, "")
    return re.sub(r"[\s·\-—]+", "", s)


def _chars(s: str) -> set[str]:
    return {ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"}


def _diff_ok(item_name: str, base_name: str) -> bool:
    """餐品名里的「差异词」必须在券名里也出现，避免 无糖/冰/热 这种误配。"""
    for w in _DIFF_WORDS:
        if w in item_name and w not in base_name:
            return False
    # 反向：券名里的「身份词」餐品名不能缺（双层券 ≠ 单层堡）
    for w in _STRICT_DIFF:
        if w in base_name and w not in item_name:
            return False
    return True


def _size_conflict(item_name: str, base_name: str) -> bool:
    """两边都标了规格且不一样 → 不是同一个东西。（冰美式大杯 ≠ 中杯冰美式）"""
    a, b = taxonomy.size_hint(item_name), taxonomy.size_hint(base_name)
    return bool(a and b and a != b)


_COUNT_RE = re.compile(r"(\d+)\s*(块|个|只|片|根|份)")


def _count_of(name: str) -> str:
    m = _COUNT_RE.search(taxonomy.normalize(name))
    return m.group(1) if m else ""


def _count_conflict(item_name: str, base_name: str) -> bool:
    """份数不一样就不是一个东西。（麦乐鸡 5 块 ≠ 4 块，营养也不同）"""
    a, b = _count_of(item_name), _count_of(base_name)
    return bool(a and b and a != b)


def _compatible(item_name: str, base_name: str) -> bool:
    """同名之外还要过三道闸：差异词、规格、份数。"""
    return (not _size_conflict(item_name, base_name)
            and not _count_conflict(item_name, base_name)
            and _diff_ok(item_name, base_name))


def _hit(name_a: str, name_b: str) -> bool:
    """券名与餐品名能否对上：严格 → 去整词规格去品牌 → 字符集包含。"""
    base = strip_coupon_words(name_a) or name_a
    item = name_b
    a, b = taxonomy.normalize(base), taxonomy.normalize(item)
    if not a or not b:
        return False
    if a == b:
        return True
    if not _compatible(item, base):
        return False
    if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
        return True
    # 宽松一级：剥掉整词规格与品牌后再比
    la, lb = _loose(base), _loose(item)
    if not la or not lb:
        return False
    if la == lb:
        return True
    ca, cb = _chars(la), _chars(lb)
    if len(la) >= 2 and len(lb) >= 2 and (ca <= cb or cb <= ca):
        return True
    return False


def strip_coupon_words(title: str) -> str:
    """从券名里剥掉营销词，留下「券作用的基础餐品名」。

    「中薯条买一送一」→「中薯条」；「王牌小食拼」→「王牌小食拼」；
    满减券返回空串（没法定位到单品）。
    """
    t = (title or "").strip()
    if not t or _THRESHOLD_RE.search(t):
        return ""
    out = t
    for w in sorted(_COUPON_SUFFIX, key=len, reverse=True):
        if w in out:
            out = out.replace(w, " ")
    out = re.sub(r"[（(][^）)]*[）)]", " ", out)
    out = re.sub(r"\s+", "", out).strip(" ·-—+满减")
    return out


def coupon_kind(title: str) -> str:
    t = title or ""
    if any(w in t for w in _BOGO_WORDS):
        return "bogo"
    if any(w in t for w in _HALF_WORDS):
        return "half"
    if _THRESHOLD_RE.search(t):
        return "threshold"
    return "discount"


# ------------------------------------------------------------------ 数据结构
@dataclass
class CouponProduct:
    code: str
    name: str


@dataclass
class CouponQuote:
    """券商品的真实算价结果（单位：元）。"""

    price: float = 0.0       # 券后应付
    origin: float = 0.0      # 券前商品价
    discount: float = 0.0    # 真实减免
    product_names: list[str] = field(default_factory=list)


@dataclass
class Coupon:
    title: str
    coupon_id: str = ""
    coupon_code: str = ""
    trade_datetime: str = ""
    promotion_id: str = ""
    products: list[CouponProduct] = field(default_factory=list)
    kind: str = "discount"
    quote: CouponQuote | None = None
    # 匹配到餐品池里的哪些餐品（由 match_pool 填）
    matched_items: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.coupon_id or self.coupon_code or self.title

    @property
    def redeem(self) -> str:
        """核销三件套，下单时原样带上。"""
        codes = [p.code for p in self.products if p.code]
        return f"{codes[0] if codes else '?'} + couponId + couponCode"

    @property
    def base_names(self) -> list[str]:
        """券到底作用在哪个餐品上。

        优先信「券商品自己的名字」—— 那是核销时真正要下单的 SKU，
        规格/份数都写死在里面（麦乐鸡 5 块 ≠ 4 块）。只有券商品名不可用时，
        才退回从券名里剥营销词。
        """
        names: list[str] = []
        for p in self.products:
            n = strip_coupon_words(p.name)
            if n and n not in names:
                names.append(n)
        if not names:
            n = strip_coupon_words(self.title)
            if n:
                names.append(n)
        return names

    @property
    def kind_label(self) -> str:
        return {
            "bogo": "买一送一", "half": "第二份半价", "threshold": "满减",
            "discount": "特惠",
        }.get(self.kind, "特惠")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "couponId": self.coupon_id,
            "couponCode": self.coupon_code,
            "promotionId": self.promotion_id,
            "tradeDateTime": self.trade_datetime,
            "kind": self.kind,
            "kindLabel": self.kind_label,
            "products": [{"code": p.code, "name": p.name} for p in self.products],
            "baseNames": self.base_names,
            "matchedItems": self.matched_items,
            "quote": (
                {
                    "price": round(self.quote.price, 2),
                    "origin": round(self.quote.origin, 2),
                    "discount": round(self.quote.discount, 2),
                }
                if self.quote
                else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Coupon":
        c = cls(
            title=d.get("title", ""),
            coupon_id=d.get("couponId", ""),
            coupon_code=d.get("couponCode", ""),
            trade_datetime=d.get("tradeDateTime", ""),
            promotion_id=d.get("promotionId", ""),
            kind=d.get("kind") or coupon_kind(d.get("title", "")),
            products=[
                CouponProduct(code=str(p.get("code", "")), name=p.get("name", ""))
                for p in (d.get("products") or [])
            ],
            matched_items=list(d.get("matchedItems") or []),
        )
        q = d.get("quote")
        if q:
            c.quote = CouponQuote(
                price=float(q.get("price", 0) or 0),
                origin=float(q.get("origin", 0) or 0),
                discount=float(q.get("discount", 0) or 0),
            )
        return c


# ------------------------------------------------------------------ 解析
def extract_business_json(text: str) -> Any | None:
    """券接口把展示用 Markdown 和业务 JSON 拼在一起，这里从后往前找业务体。"""
    if not isinstance(text, str):
        return text if isinstance(text, (dict, list)) else None
    decoder = json.JSONDecoder()
    found: Any | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("data" in obj or "success" in obj):
            found = obj
    return found


def parse_coupons(raw: Any) -> list[Coupon]:
    """解析 query-store-coupons 返回，得到券列表。"""
    payload = raw
    if isinstance(payload, str):
        payload = extract_business_json(payload)
    if isinstance(payload, dict):
        data = payload.get("data", payload)
    else:
        data = payload
    if isinstance(data, dict):
        data = data.get("list") or data.get("coupons") or []
    if not isinstance(data, list):
        return []

    out: list[Coupon] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("name") or row.get("couponName") or ""
        if not title:
            continue
        products = []
        for p in row.get("products") or []:
            if isinstance(p, dict):
                products.append(
                    CouponProduct(code=str(p.get("productCode") or p.get("code") or ""),
                                  name=p.get("productName") or p.get("name") or "")
                )
            elif isinstance(p, str):
                products.append(CouponProduct(code="", name=p))
        out.append(
            Coupon(
                title=title,
                coupon_id=str(row.get("couponId") or ""),
                coupon_code=str(row.get("couponCode") or row.get("code") or ""),
                trade_datetime=str(row.get("tradeDateTime") or ""),
                promotion_id=str(row.get("promotionId") or ""),
                products=products,
                kind=coupon_kind(title),
            )
        )
    return out


def fetch_coupons(
    client: McdMcpClient,
    store_code: str,
    order_type: int = 1,
    be_type: int = 1,
    be_code: str | None = None,
) -> list[Coupon]:
    """查门店当前可用券。失败不抛错（券只是加分项，不该拖垮主流程）。"""
    args: dict[str, Any] = {
        "storeCode": store_code, "orderType": order_type, "beType": be_type,
    }
    if be_code:
        args["beCode"] = be_code
    try:
        return parse_coupons(client.tool("query-store-coupons", args))
    except McpError:
        return []


# ------------------------------------------------------------------ 待领取的券
@dataclass
class ClaimableCoupon:
    title: str
    claimed: bool = False
    image: str = ""


_CLAIMED_STATUS = ("HAVING_RECEIVE", "RECEIVED", "USED", "EXPIRED", "HAVING_USE")


def parse_claimable(raw: Any) -> list[ClaimableCoupon]:
    """解析 available-coupons。

    这个接口两种形态都遇到过：structuredContent 里是
    `data[].{couponName,couponStatus,label}`，纯文本时是 Markdown 列表。
    两种都认，别只写一种。
    """
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        if isinstance(data, dict):
            data = data.get("list") or data.get("coupons") or []
        if isinstance(data, list):
            out: list[ClaimableCoupon] = []
            for x in data:
                if not isinstance(x, dict):
                    continue
                title = str(x.get("couponName") or x.get("title") or x.get("name") or "").strip()
                if not title:
                    continue
                status = str(x.get("couponStatus") or "").upper()
                label = str(x.get("label") or x.get("status") or "")
                claimed = status in _CLAIMED_STATUS or ("已领取" in label) or ("已使用" in label)
                out.append(ClaimableCoupon(
                    title=title, claimed=claimed,
                    image=str(x.get("couponImage") or x.get("image") or ""),
                ))
            if out:
                return out

    text = raw if isinstance(raw, str) else ""
    out = []
    cur: ClaimableCoupon | None = None
    for line in text.splitlines():
        s = line.strip().rstrip("\\").strip()
        if "优惠券标题" in s:
            title = s.split("：", 1)[-1].split(":", 1)[-1].strip()
            cur = ClaimableCoupon(title=title)
            out.append(cur)
        elif cur is not None and s.startswith("状态"):
            state = s.split("：", 1)[-1].split(":", 1)[-1].strip()
            cur.claimed = "已领取" in state or "已使用" in state
        elif cur is not None and "<img" in s:
            i = s.find('src="')
            if i >= 0:
                cur.image = s[i + 5:].split('"', 1)[0]
    return [c for c in out if c.title]


def fetch_claimable(client: McdMcpClient) -> list[ClaimableCoupon]:
    try:
        return parse_claimable(client.tool("available-coupons"))
    except McpError:
        return []


def claim_all(client: McdMcpClient) -> str:
    """一键领取麦麦省里当前可领的券。会改变用户账号状态，需用户明确同意后调用。"""
    try:
        raw = client.tool("auto-bind-coupons")
    except McpError as exc:
        return f"领券失败：{exc}"
    if isinstance(raw, str):
        return raw.strip()[:400] or "已提交领券请求"
    if isinstance(raw, dict):
        msg = raw.get("message") or raw.get("msg")
        n = raw.get("data") if not isinstance(raw.get("data"), dict) else None
        if msg:
            return f"{msg}" + (f"（{n}）" if n is not None else "")
    return "已提交领券请求"


# ------------------------------------------------------------------ 与餐品池对齐
def match_pool(coupons: Sequence[Coupon], pool: Iterable[FoodItem]) -> dict[str, list[str]]:
    """把每张券对到餐品池里能用的餐品，返回 {券key: [餐品名]}，并回填 matched_items。"""
    items = list(pool)
    hits: dict[str, list[str]] = {}
    for c in coupons:
        names: list[str] = []
        for base in c.base_names:
            for it in items:
                if _hit(base, it.name) and it.name not in names:
                    names.append(it.name)
        # 买一送一常写成「券 SKU 名」，兜底再拿券商品名与菜单名对一次
        if not names and c.kind in ("bogo", "half"):
            for p in c.products:
                for it in items:
                    if _hit(p.name, it.name) and it.name not in names:
                        names.append(it.name)
        c.matched_items = names
        hits[c.key] = names
    return hits


def matched_items(coupons: Sequence[Coupon]) -> set[str]:
    out: set[str] = set()
    for c in coupons:
        out.update(c.matched_items)
    return out


def coupon_index(coupons: Sequence[Coupon]) -> dict[str, Coupon]:
    """餐品名 → 最划算的那张券（同餐品多张券取减免最大的）。"""
    idx: dict[str, Coupon] = {}
    for c in coupons:
        for name in c.matched_items:
            cur = idx.get(name)
            if cur is None:
                idx[name] = c
                continue
            a = (c.quote.discount if c.quote else 0.0)
            b = (cur.quote.discount if cur.quote else 0.0)
            if a > b:
                idx[name] = c
    return idx


# ------------------------------------------------------------------ 试算
def quote_coupon(
    client: McdMcpClient,
    coupon: Coupon,
    store_code: str,
    order_type: int = 1,
    be_type: int = 1,
    be_code: str | None = None,
    reservation_date: str | None = None,
) -> CouponQuote | None:
    """用麦当劳自己的算价引擎试算券商品的券后价。"""
    codes = [p.code for p in coupon.products if p.code]
    if not codes:
        return None
    item: dict[str, Any] = {"productCode": codes[0], "quantity": 1}
    if coupon.coupon_id:
        item["couponId"] = coupon.coupon_id
    if coupon.coupon_code:
        item["couponCode"] = coupon.coupon_code
    args: dict[str, Any] = {
        "storeCode": store_code, "orderType": order_type, "beType": be_type,
        "items": [item],
    }
    if be_code:
        args["beCode"] = be_code
    if reservation_date:
        args["reservationDate"] = reservation_date
    try:
        raw = client.tool("calculate-price", args)
    except McpError:
        return None
    if isinstance(raw, str):
        raw = extract_business_json(raw)
    if not isinstance(raw, dict):
        return None
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        return None
    price = float(data.get("price", 0) or 0) / 100
    product = float(data.get("productPrice", 0) or 0) / 100
    discount = float(data.get("discount", 0) or 0) / 100
    if price <= 0 and discount <= 0:
        return None  # 这张券当前组合核销不了
    return CouponQuote(
        price=price,
        origin=round(product + discount, 2),
        discount=discount,
        product_names=[p.get("productName", "") for p in (data.get("productList") or [])],
    )


def enrich_quotes(
    client: McdMcpClient,
    coupons: Sequence[Coupon],
    store_code: str,
    *,
    order_type: int = 1,
    be_type: int = 1,
    be_code: str | None = None,
    only_matched: bool = True,
    limit: int = 12,
) -> list[Coupon]:
    """给券补齐真实算价（只算能对上餐品的，省调用）。"""
    todo = [c for c in coupons if (c.matched_items if only_matched else True)][:limit]
    for c in todo:
        q = quote_coupon(client, c, store_code, order_type, be_type, be_code)
        if q:
            c.quote = q
    return list(coupons)


# ------------------------------------------------------------------ 命中
@dataclass
class CouponHit:
    """一张券作用在方案里的某个餐品上。

    这里必须区分两种截然不同的「划算」，混淆了就是骗人：
    - **现金减免（mode="cash"）**：同样的东西付更少的钱。
    - **同价加量（mode="bonus"）**：买一送一/第二份半价，钱不少花，
      但你多拿到一份东西 —— 那份额外热量和钠必须如实报出来。
    实测数据支撑：单独算「中薯条买一送一」→ 商品价 13.5、优惠 13.5，
    也就是花一份钱拿两份，应付并没有下降。
    """

    slot: str
    item_name: str
    coupon_title: str
    coupon_id: str
    coupon_code: str
    kind_label: str
    mode: str = "cash"          # cash | bonus
    unit_price: float = 0.0     # 方案里这一份的单价
    list_price: float = 0.0     # 同样的内容不用券要花多少
    paid: float = 0.0           # 用券后实付
    saving: float = 0.0         # 现金少付（mode=cash 时才有值）
    bonus_units: float = 0.0    # 多送几份（买一送一 = 1，第二份半价 = 0.5）
    bonus_value: float = 0.0    # 多送部分折成钱值多少
    bonus_kcal: float = 0.0     # 多送部分带来的额外能量
    bonus_sodium: float = 0.0
    redeem_code: str = ""
    note: str = ""

    @property
    def head_line(self) -> str:
        if self.mode == "cash" and self.saving > 0:
            return f"¥{self.list_price:.1f} → ¥{self.paid:.1f}（少付 ¥{self.saving:.1f}）"
        if self.bonus_units > 0:
            return (f"¥{self.paid:.1f} 不变，多送 {self.bonus_units:g} 份"
                    f"（价值 ¥{self.bonus_value:.1f}）")
        return f"券后 ¥{self.paid:.1f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot, "item": self.item_name, "couponTitle": self.coupon_title,
            "couponId": self.coupon_id, "couponCode": self.coupon_code,
            "kindLabel": self.kind_label, "mode": self.mode,
            "unitPrice": round(self.unit_price, 2), "listPrice": round(self.list_price, 2),
            "origin": round(self.list_price, 2),   # 兼容字段名
            "paid": round(self.paid, 2), "saving": round(self.saving, 2),
            "bonusUnits": self.bonus_units, "bonusValue": round(self.bonus_value, 2),
            "bonusKcal": round(self.bonus_kcal, 1), "bonusSodium": round(self.bonus_sodium, 1),
            "redeemProductCode": self.redeem_code, "note": self.note,
            "headLine": self.head_line,
        }


def hits_for_plan(plan: Any, coupons: Sequence[Coupon]) -> list[CouponHit]:
    """算出某套方案里所有可用的券，并按性质区分现金减免 / 同价加量。

    现金减免额与券商品价格都取麦当劳算价接口的真实返回；
    买一送一这类按「减免 ÷ 单价」推出多送几份，并如实折算额外能量与钠。
    """
    idx = coupon_index(coupons)
    out: list[CouponHit] = []
    for slot in plan.bundles:
        for it in plan.bundles[slot].items:
            c = idx.get(it.name)
            if c is None or c.quote is None:
                continue
            unit = float(it.price or 0.0)
            paid = float(c.quote.price or 0.0)
            origin = float(c.quote.origin or 0.0)
            discount = float(c.quote.discount or 0.0)

            bonus = 0.0
            if c.kind in ("bogo", "half") and unit > 0:
                bonus = round(discount / unit, 2)

            if bonus > 0:
                mode = "bonus"
                saving = 0.0
                bonus_value = discount
                list_price = unit
                paid_eff = unit
                note = (f"多送 {bonus:g} 份{it.name}：价值 ¥{discount:.1f}，"
                        f"额外 {it.kcal * bonus:.0f} kcal、{it.sodium * bonus:.0f} mg 钠")
            else:
                mode = "cash"
                list_price = max(origin, unit)
                paid_eff = paid if paid > 0 else unit
                saving = max(list_price - paid_eff, 0.0)
                bonus_value = 0.0
                note = f"实付 ¥{paid_eff:.1f}，比不用券少花 ¥{saving:.1f}"

            out.append(
                CouponHit(
                    slot=slot, item_name=it.name, coupon_title=c.title,
                    coupon_id=c.coupon_id, coupon_code=c.coupon_code,
                    kind_label=c.kind_label, mode=mode, unit_price=unit,
                    list_price=list_price, paid=paid_eff, saving=saving,
                    bonus_units=bonus, bonus_value=bonus_value,
                    bonus_kcal=it.kcal * bonus, bonus_sodium=it.sodium * bonus,
                    redeem_code=(c.products[0].code if c.products else ""), note=note,
                )
            )
    return out


def attach_to_plans(plans: Sequence[Any], coupons: Sequence[Coupon]) -> None:
    """给每套方案挂上券命中、现金减免与券后实付（原地修改）。"""
    for p in plans:
        hits = hits_for_plan(p, coupons)
        p.coupon_hits = hits
        p.coupon_saving = round(sum(h.saving for h in hits), 2)
        p.coupon_extra_value = round(sum(h.bonus_value for h in hits), 2)
        p.net_price = round(max(p.price - p.coupon_saving, 0.0), 2)


def clone(coupons: Sequence[Coupon]) -> list[Coupon]:
    return [copy.copy(c) for c in coupons]


def dumps(coupons: Sequence[Coupon]) -> str:
    return json.dumps([c.to_dict() for c in coupons], ensure_ascii=False, indent=1)


def loads(text: str) -> list[Coupon]:
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [Coupon.from_dict(r) for r in rows if isinstance(r, dict)]

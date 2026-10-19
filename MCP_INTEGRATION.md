# MCP 集成说明

本文件说明「看见什么吃什么」实际使用了麦当劳 MCP Server 的哪些能力、怎么调用、以及各自创造的业务价值。

- **MCP Server**：麦当劳中国 MCP Server（`https://mcp.mcd.cn`，Streamable HTTP）
- **接入文档**：<https://github.com/M-China/mcd-mcp-server>
- **鉴权**：`Authorization: Bearer ${MCD_MCP_TOKEN}`，Token 仅从环境变量读取，不落盘、不进日志
- **客户端实现**：`src/seeandeat/mcp_client.py`，纯标准库实现（无 MCP SDK 依赖），
  兼容服务端直接返回 JSON 与 SSE 流式两种响应

---

## 一、用到的 Tool 总览

| # | Tool | 在本项目中的作用 | 是否核心 |
| --- | --- | --- | --- |
| 1 | `list-nutrition-foods` | 全部餐品营养数据（能量/蛋白质/脂肪/碳水/钠/钙） | ✅ 核心 |
| 2 | `query-nearby-stores` | 定位用户可点餐门店，拿 `storeCode` | ✅ 核心 |
| 3 | `query-meals` | 门店当前可售卖餐品与价格，**按早/午/晚分时拉取** | ✅ 核心 |
| 4 | `now-time-info` | 取服务器时间，决定各餐次该预约到哪一天 | ✅ 核心 |
| 5 | `query-meal-detail` | 查餐品规格与特调选项（下单前核对规格） | 辅助 |
| 6 | `query-store-coupons` | 查该门店可用优惠券与**券商品编码** | ✅ 核心 |
| 7 | `calculate-price` | 算商品金额、打包费、配送费、优惠与应付；**带券实测券后价** | ✅ 核心 |
| 8 | `create-order` | 创建真实订单并返回支付链接 | ✅ 下单链路 |
| 9 | `available-coupons` / `auto-bind-coupons` | 麦麦省可领券查询 / 一键领券（`scripts/coupons.py --claim`） | ✅ 券链路 |
| 10 | `delivery-query-addresses` / `delivery-query-stores` | 外送场景选地址与门店（预留） | 可选 |

---

## 二、调用流程

```
① now-time-info               取服务器当前时间
        │
② query-nearby-stores         定位门店 → storeCode
        │
③ list-nutrition-foods        一次性拉全量营养表（160 条）
        │
④ query-meals × 3             按早/午/晚分别带 reservationDate 拉分时菜单
        │                     （早餐 08:30 / 午餐 12:00 / 晚餐 18:30）
        ▼
   双向对齐：营养表 ⟷ 在售菜单
   · 以营养表条目为规划单位（规格最精确）
   · 反查菜单拿价格与 productCode
   · 剔除捆绑套餐与营养表未覆盖的限定新品，绝不估算
        │
⑤ query-store-coupons         查门店可用券（含券商品编码）
        │
⑥ calculate-price × N         逐张试算券后价，判定「少付钱」还是「同价多拿」
        ▼
   券 ⟷ 餐品池对齐（严格匹配：冰/热、大中小杯、份数对不上就不算）
        │
⑦ 束搜索组合优化（本地）
   目标 = 整日营养偏离 + 供能比 + 重复度 + 换一批回避 + 券偏好
        ▼
   三套方案（含券后价与划算方式）+ Markdown / HTML 报告
        │
        ├─⑧ calculate-price       整单算价（可带 couponId/couponCode）
        └─⑨ create-order          下单（需用户确认）
```

### 调用示例

```jsonc
// ④ 早餐菜单：必须带 reservationDate，否则麦当劳只返回「当前时段」菜单，
//    13:00 去查是拿不到麦满分的
{
  "name": "query-meals",
  "arguments": {
    "storeCode": "1420621",
    "orderType": 1,
    "beType": 1,
    "reservationDate": "2026-10-17 08:30"
  }
}
```

```jsonc
// ⑤ 查门店可用券：返回 data[].{title, couponId, couponCode, products[].productCode}
{
  "name": "query-store-coupons",
  "arguments": { "storeCode": "1420621", "orderType": 1, "beType": 1 }
}
```

```jsonc
// ⑥ 核销券试算（关键！见下面「已知的接口边界」第 4 条）
//    items[].productCode 必须是「券商品自己的编码」，并同时带 couponId + couponCode
{
  "name": "calculate-price",
  "arguments": {
    "storeCode": "1420621",
    "orderType": 1,
    "beType": 1,
    "items": [{
      "productCode": "9900012387",              // 券商品「中薯条买一送一」
      "quantity": 1,
      "couponId": "8692944B045928451618BFF22F535D2A",
      "couponCode": "MCD60N1317590217R007B"
    }]
  }
}
// 返回：productPrice 13.5、discount 13.5 → 花一份钱拿两份中薯条（应付 13.5，不是 0）
```

```jsonc
// ⑦ 整单算价（字段名是 productCode，金额单位是「分」）
{
  "name": "calculate-price",
  "arguments": {
    "storeCode": "1420621",
    "orderType": 1,
    "beType": 1,
    "items": [
      {"productCode": "1600", "quantity": 1},
      {"productCode": "4810", "quantity": 1}
    ]
  }
}
```

---

## 三、每个 Tool 的业务价值

### 1. `list-nutrition-foods` —— 让「健康」变成可计算的东西

没有它，「麦当劳健不健康」只能停留在口号。有了 160 条真实营养数据，
才能把一顿饭拆成能量、蛋白质、脂肪、碳水、钠、钙六个维度，
逐项与《中国居民膳食指南》和 DRIs 的目标对比，得出「哪里够、哪里爆」的具体结论。

**这是整个项目的地基。**

### 2. `query-meals` + `reservationDate` —— 保证「配得出来就点得到」

这是本项目最依赖 MCP 的一个细节：麦当劳分时段供应，
直接查「当前菜单」在下午是拿不到早餐的；而只查「明天早餐」又配不出晚餐。

本项目按早/午/晚分别带 `reservationDate` 调用三次，
得到三个时段的真实可售餐品与价格。加上与营养表的双向对齐，
**凡是出现在方案里的餐品，都是这家门店这个时段真的点得到的。**

### 3. `now-time-info` —— 决定「今天」还是「明天」

取服务器时间后，若某餐次的时间点今天已过，就自动顺延到明天，
避免给用户在 19:00 推荐一份明早就已售罄的早餐。

### 4. `query-store-coupons` × `calculate-price` —— 把优惠券变成「可计算的省钱」

大多数用法是「查一下有什么券」，本项目把它做成了**券进入搭配算法的输入**：

1. `query-store-coupons` 返回的每张券都带**券商品编码**（`products[].productCode`）；
2. 用券商品编码调用 `calculate-price` 试算，拿到真实的 `productPrice` / `discount` / `price`；
3. 把券与餐品池对齐（严格匹配，见下），命中的餐品在打分时获得偏好；
4. 说「换一批」时**有券的餐品不做回避** —— 优先给用户真正能省钱的组合。

**关键发现：优惠券有两种完全不同的「划算」，混为一谈就是误导。**

| 类型 | `calculate-price` 表现 | 能不能少付钱 |
| --- | --- | --- |
| 特惠 / 立减 | `price` < 券前价 | ✅ 同样的东西少付钱 |
| 买一送一 / 第二份半价 | `price` 不变，`discount` 记为被送掉那份的价值 | ❌ 价格不变，但多给你一份 |

实测「中薯条买一送一」：`productPrice` 13.5、`discount` 13.5、`price` 13.5 ——
**花一份钱拿两份**，不是 27 元减到 13.5 元。所以本项目的报告会把「多送的那份」
折算成额外能量与钠一并列出，绝不把它当折扣讲。

### 5. `available-coupons` + `auto-bind-coupons` —— 券不够就先去领

门店可用券少的时候（例如只有 1 张能对上餐品），先看看麦麦省有没有可领的：

```bash
python scripts/coupons.py --claim      # 一键领取，会写入用户账号，必须先向用户说明
```

实测：运行前门店只有 2 张券（1 张可用），领取后变成 3 张，
多出「9.9元中杯冰美式」。虽然它因为「当天菜单只有热美式、券是冰美式」
而被判定用不上，但这条链路本身是通的 —— 而**判定用不上正是我们想要的行为**。

### 6. `query-meal-detail` —— 规格核对

`可乐中杯/大杯`、`薯条小/中/大` 营养差很多，规划时按具体规格算，
下单前用它核对规格与特调选项。

---

## 四、调用频次与限流

麦当劳 MCP 限制 **600 次/分钟**，本项目一次完整配餐的请求数：

| 场景 | 请求数 |
| --- | --- |
| 配三餐（`plan.py`，含券） | 6 + 1 + N（时间 1 + 门店 1 + 营养 1 + 菜单 3 + 查券 1 + 券后价试算 N 张） |
| 配三餐（`--no-coupon`） | 6 |
| 换一批（`--reroll`） | 同上（批次记忆在本地，不额外请求） |
| 下单（`order.py --use-coupon`） | 2–3（查券 + 算价 + 下单） |

N = 能对上餐品的券数量，实测 1–3 张，所以一次配餐通常 8–12 次请求，远低于限流阈值。
`mcp_client.py` 对 401 / 429 有明确中文提示；
券相关的任何一步失败都不会中断配餐（券只是加分项，不是必需项）。

---

## 五、已知的接口边界（如实记录）

1. **`calculate-price` 对多规格餐品按默认规格计价。**
   例：可乐的规格选项是独立编码（`可乐大杯` = `3100`），
   但把 `3100` 作为 `productCode` 传入返回 0；传父编码（`9900008751`）则按默认小杯计价。
   → 本项目规划时按营养表的具体规格计算营养，同时在报告与下单脚本中明确提示
   「杯型/份量请在麦当劳 App 上确认」。

2. **营养接口不含膳食纤维与维生素。**
   膳食指南推荐每天 25–30 g 膳食纤维，但接口只提供六项营养素。
   → 本项目不估算，只在报告中提醒用户自行补充蔬菜水果。

3. **捆绑套餐（三件套、随心配等）营养无法拆分。**
   → 不参与搭配，在报告「未纳入搭配的餐品」中明确列出并说明原因。

4. **券必须用「券商品的 productCode」核销，且要带 `couponId` + `couponCode`。**
   用普通餐品编码 + 券参数会返回 0；只传券商品编码不带券参数也返回 0。
   这是本项目的实测结论（官方 Tool 描述里 `couponId`/`couponCode` 是选填，
   但实测必须与券商品编码配套使用）。→ `scripts/order.py --use-coupon` 已封装三件套。

5. **有些券对不上「有营养数据的在售单品」。**
   例如「王牌小食拼」的券商品是套餐类 SKU，营养表没有可拆分的对应条目。
   → 这类券不参与搭配，只在报告「有券的组合」里说明「对不上餐品」。

6. **券的可用性随门店、时段、账号变化。**
   → 报告里的券后价是生成报告那一刻的实测值；
   `data/coupons_snapshot.json` 里的券后价也标注了采集时间。

7. **同一编码可能对应多个规格。**
   菜单若只列通用名「薯条」（编码 4810），营养表里的 `迷你薯条/中薯条/大薯条`
   会反查到同一个编码。→ 下单时以麦当劳 App 内的规格选择为准，报告已提示。

---

## 六、配置示例（脱敏）

见 `mcp-config.example.json`，Token 一律用占位符：

```jsonc
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

本项目代码中 Token 只通过 `os.environ["MCD_MCP_TOKEN"]` 读取，
仓库中不含任何真实 Token、密钥或账号凭证。

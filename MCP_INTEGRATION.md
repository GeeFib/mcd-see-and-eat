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
| 6 | `query-store-coupons` | 查该门店可用优惠券 | ✅ 下单链路 |
| 7 | `calculate-price` | 算商品金额、打包费、配送费、优惠与应付 | ✅ 下单链路 |
| 8 | `create-order` | 创建真实订单并返回支付链接 | ✅ 下单链路 |
| 9 | `available-coupons` / `auto-bind-coupons` | 麦麦省领券（`scripts/` 中预留入口） | 可选 |
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
⑤ 束搜索组合优化（本地）
        ▼
   三套方案 + Markdown / HTML 报告
        │
        ├─⑥ query-store-coupons   查可用券
        ├─⑦ calculate-price       算价
        └─⑧ create-order          下单（需用户确认）
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
// ⑦ 算价（注意字段名是 productCode，金额单位是「分」）
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

### 4. `query-store-coupons` → `calculate-price` → `create-order` —— 从建议到行动

营养搭配如果只是「建议」，用户还是要自己打开 App 一个一个点。
接上这三个 Tool 后，方案里的餐品编码可以直接算价、看券、下单，
**把「今天吃什么」这个决策完整闭环掉。**

### 5. `query-meal-detail` —— 规格核对

`可乐中杯/大杯`、`薯条小/中/大` 营养差很多，规划时按具体规格算，
下单前用它核对规格与特调选项。

---

## 四、调用频次与限流

麦当劳 MCP 限制 **600 次/分钟**，本项目一次完整配餐的请求数：

| 场景 | 请求数 |
| --- | --- |
| 配三餐（`plan.py`） | 6（时间 1 + 门店 1 + 营养 1 + 菜单 3） |
| 下单（`order.py`） | 2–3（查券 + 算价 + 下单） |

远低于限流阈值。`mcp_client.py` 对 401 / 429 有明确中文提示。

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

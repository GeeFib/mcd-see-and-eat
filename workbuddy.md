# WorkBuddy 开发上下文

> 本文件用于核验「WorkBuddy 专项奖励」条件：记录本项目在腾讯 WorkBuddy 智能体中的开发过程。
> 导出时间：2026-10-16

## 一、项目信息

- **项目名称**：看见什么吃什么（mcd-see-and-eat）
- **开发平台**：腾讯 WorkBuddy（Agent 模式）
- **基座能力**：麦当劳中国 MCP Server（`https://mcp.mcd.cn`）
- **参赛活动**：麦当劳程序员节创意开发大赛（M-China/mcd-developer-innovation-challenge）

## 二、需求原文

> 参与这个挑战。我的 MCP Token：`<已脱敏>`
> 我想做一个"看见什么吃什么的skill"，为每日悬着困难症的打工人搭配营养餐。
> 根据国家膳食指南、和麦当劳当前可售卖的餐品列表，以及其营养信息，
> 搭配每日三餐需要的能量、蛋白质、脂肪、碳水化合物、钠、钙等，搭配健康的餐食。并推荐给用户

## 三、开发过程（WorkBuddy 会话摘要）

### 1. 读规则
抓取活动仓库 README、activityGuidelines、CONTEST_DECLARATION，
确认必须文件：`README.md`、`CONTEST_DECLARATION.md`、`MCP_INTEGRATION.md`、
`mcp-config.example.json`、`workbuddy.md`（WorkBuddy 专项奖励）。

### 2. 摸清 MCP 能力
用脚本直连 `https://mcp.mcd.cn` 做 JSON-RPC `tools/list`，拿到全部 30+ Tool 及入参 schema。
确认关键 Tool：
- `list-nutrition-foods`（无入参，返回 160 条餐品营养）
- `query-meals`（需 storeCode + orderType + beType，支持 `reservationDate`）
- `query-nearby-stores`、`now-time-info`
- `calculate-price` / `create-order` / `query-store-coupons`

### 3. 真实数据验证
- `list-nutrition-foods` → 160 条，字段为 `productName,kj,kcal,protein,fat,carb,sodium,calcium` 的紧凑文本
- `query-nearby-stores` → 麦当劳深圳蛇口网谷餐厅 `1420621`
- `query-meals` 当前时段 → 106 个在售餐品（含价格与编码）

### 4. 踩到的关键坑：麦当劳分时段供应
第一次用「当前菜单」配餐时发现**早餐餐品一条都拿不到**——下午 13:00 查菜单，
麦当劳早已切成正餐菜单。
→ 解法：`query-meals` 带 `reservationDate` 分别拉早/午/晚三个时段的菜单。
验证结果：早餐 08:30 → 83 个餐品（麦满分系列全在），午餐 12:00 → 106 个，晚餐 18:30 → 105 个。

### 5. 数据对齐方案迭代
- 第一版：以「在售菜单」为主单位反查营养表 → 只有 34 条可用，且「巨无霸」被分错类
- 第二版：改为**以营养表条目为主单位**（规格最精确，如 `可乐中杯`/`大薯条`），
  反查菜单拿价格与编码；同时修正分类规则（主食判定必须先于小食，否则「麦辣鸡腿汉堡」会被「鸡腿」抢走）
- 结果：72 条可用餐品，无「未分类」

### 6. 搭配算法调优
- 第一版：每餐能量系统性偏低 15%（优化器为压脂肪比而少给）→ 加「单餐能量 ±25% 带过滤」
- 第二版：三套方案长得几乎一样 → 预设不只改权重，还**直接改目标值**
  （高蛋白把蛋白质目标 ×1.35，低钠把钠上限 ×0.85）
- 第三版：同一餐品一天点两遍 → 重复度惩罚权重从 0.35 提到 0.8

### 7. 下单链路验证
- `calculate-price` 的 items 字段名是 **`productCode` 不是 `code`**（传 `code` 会静默返回 0 元），金额单位是「分」
- 多规格餐品（如可乐大杯编码 `3100`）作为 `productCode` 传入返回 0，
  传父编码 `9900008751` 则按默认规格计价
  → 结论：当前 MCP 无法表达规格选择，已在报告与文档中如实说明

### 8. 交付
- `scripts/plan.py`：配餐主入口，实测 2–4 秒出三套方案
- `scripts/order.py`：查券 → 算价 → 下单（`--confirm` 才真下单）
- `scripts/refresh_menu.py`：刷新本地快照
- 生成 `demo/sample-report.html`（真实门店实时数据）
- 已安装为 WorkBuddy Skill：`~/.workbuddy/skills/mcd-see-and-eat/`
- 已配置 MCP 连接器：`~/.workbuddy/mcp.json` 中新增 `mcd-mcp`

## 四、WorkBuddy 在本项目中的作用

| 环节 | WorkBuddy 做了什么 |
| --- | --- |
| 需求拆解 | 从一句话需求拆出「营养目标 / 数据对齐 / 组合优化 / 报告 / 下单」五层 |
| MCP 探活 | 直连 MCP 拿 Tool 列表与 schema，逐个试出真实入参（如 `productCode`） |
| 数据验证 | 拉取并交叉比对 160 条营养 × 3 个时段菜单，定位分时段供应问题 |
| 算法调优 | 三轮迭代：能量带过滤 → 预设改目标值 → 重复度惩罚 |
| 交付打包 | 生成 Skill 包、参赛文档、HTML/Markdown 报告 |

## 五、实测输出示例

门店：麦当劳深圳蛇口网谷餐厅（1420621）　画像：男 / 28 岁 / 175cm / 72kg / 久坐办公 / 维持体重

| 方案 | 能量 | 蛋白质 | 脂肪 | 碳水 | 钠 | 钙 | 价格 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 均衡日常 | 2257 kcal | 81 g | 75 g | 300 g | 2256 mg | 841 mg | ¥141.5 |
| 高蛋白保肌 | 2314 kcal | 94 g | 78 g | 297 g | 2478 mg | 888 mg | ¥142.5 |
| 低钠轻负担 | 1779 kcal | 69 g | 57 g | 236 g | 1984 mg | 658 mg | ¥121.0 |

目标：能量 2350 kcal / 蛋白质 72 g / 钠 ≤2000 mg / 钙 800 mg。

## 六、声明

本项目由开发者在腾讯 WorkBuddy 中独立完成，非麦当劳官方产品。
输出仅供参考，不构成医疗、营养或专业建议；餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。

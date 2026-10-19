---
name: mcd-see-and-eat
description: "看见什么吃什么：按《中国居民膳食指南》和麦当劳实时在售菜单，给选择困难症打工人配一日三餐（能量/蛋白质/脂肪/碳水/钠/钙），输出可直接下单的方案。当用户说「今天吃什么」「帮我配麦麦三餐」「麦当劳怎么吃才健康」「按我的身高体重配餐」「减脂期能吃麦当劳吗」时使用。"
---

# 看见什么吃什么 · 麦当劳营养搭配

给「每天中午站在菜单前发呆十分钟」的打工人一个确定性答案：
按中国居民膳食指南算出你一天该吃多少，再用麦当劳**当前这家门店真的卖**的餐品配出来。

## 前置条件

1. 麦当劳 MCP Token（到 https://open.mcd.cn/mcp 登录申请），配置到环境变量：

   ```bash
   export MCD_MCP_TOKEN=<你的 Token>
   ```

   或在 WorkBuddy 里把 `mcd-mcp` 连接器启用（配置见仓库 `mcp-config.example.json`）。
2. Python 3.9+，无第三方依赖。

没有 Token 也能跑：加 `--offline`，用仓库自带的快照演示。

## 工作流

### 第 1 步：先问清四件事（别猜）

向用户确认，缺失时才用默认值并在最后说明：

| 项 | 必问 | 默认 |
| --- | --- | --- |
| 性别 / 年龄 / 身高 / 体重 | ✅ 体重和身高必须有 | — |
| 日常活动量 | 久坐办公 / 轻度 / 中度 / 高强度 | 久坐办公 |
| 目标 | 减脂 / 维持 / 增肌 | 维持 |
| 哪几餐在麦当劳解决 | 早/午/晚，可多选 | 三餐 |

有忌口（比如不喝可乐、不吃牛肉）也一并用 `--exclude` 传进去。

### 第 2 步：跑搭配

```bash
cd <skill 目录>
python scripts/plan.py \
  --sex male --age 28 --height 175 --weight 72 \
  --activity sedentary --goal maintain \
  --meals breakfast,lunch,dinner \
  --html today.html --md today.md --json today.json
```

常用参数：
- `--meals lunch,dinner`：只在麦当劳吃两餐（更现实，钠也更容易压住）
- `--goal cut`：减脂；`--goal bulk`：增肌
- `--preset low_sodium`：只要低钠方案
- `--exclude 可乐,牛肉`：忌口
- `--offline`：不联网，用快照

耗时约 2-4 秒，会依次调用 MCP 的 `query-nearby-stores` / `now-time-info` /
`list-nutrition-foods` / `query-meals`（按早中晚三个时段各拉一次）。

### 第 3 步：给用户看

优先把 `--html` 生成的报告用 present_files 打开（手机也能看）。
在回复里只讲三件事，**不要**把整张营养表复述一遍：

1. 今天吃什么（三餐各点哪几样、多少钱）
2. 达标情况（能量、蛋白质、钙够不够；钠超没超）
3. 一句大实话——**麦当劳三餐的钠基本都会顶到 2000mg 上限**，
   建议另外多吃蔬果补钾、多喝水；膳食纤维麦当劳本身吃不够，要自己补。

### 第 4 步：下单（用户明确说要才做）

```bash
python scripts/order.py --plan today.json --meal lunch --preset balanced   # 只算价，看券
python scripts/order.py --plan today.json --meal lunch --preset balanced --confirm  # 真下单
```

`--confirm` 会创建真实订单并扣款，必须先向用户复述菜品与应付金额并得到确认。

## 输出解读

- 三套方案不是随机扰动，是三种明确取舍：
  - **均衡日常**：全面贴近膳食指南，闭眼选这个
  - **高蛋白保肌**：蛋白质目标抬到约 1.35 g/kg，训练日/减脂期用
  - **低钠轻负担**：把钠压到最低，代价是能量和钙会低一些
- 「达成率」低于 100% 对钠、脂肪是好事（越低越好），对能量、蛋白质、钙则相反。

## 已知边界（要如实告诉用户）

- 麦当劳营养接口只给能量、蛋白质、脂肪、碳水、钠、钙六项，**没有膳食纤维和维生素**数据。
- **三餐全吃麦当劳，钠几乎必然 ≥2000mg**（单个主食就 500-1200mg）。
  真要控钠，请用 `--meals lunch` 只规划一餐，其余在家吃。
- 多规格餐品（可乐大杯、大薯条等）营养按具体规格计算，
  但 MCP 的 `calculate-price` 目前按默认规格计价，杯型/份量请在麦当劳 App 上确认。
- 捆绑套餐（三件套、随心配）营养无法拆分，不参与搭配；营养表没覆盖的限定新品同样剔除。
- 这是膳食搭配建议，不是医嘱；有高血压、肾病、糖尿病等基础疾病请遵医嘱。

## 文件

- `scripts/plan.py` 主入口；`scripts/order.py` 下单；`scripts/refresh_menu.py` 刷新快照
- `src/seeandeat/` 核心：`dri.py`（目标）、`menu.py`（数据对齐）、`planner.py`（组合优化）、`report.py`（报告）
- `references/dietary_guidelines.md` 目标值的出处与推导

# 🍔 看见什么吃什么

> 每天中午站在麦当劳菜单前发呆十分钟，最后还是点了上次那个。
> 这个 Skill 替你把这事想清楚：**按《中国居民膳食指南》算出你一天该吃多少，
> 再用这家门店当前真的在卖的餐品配出来，顺手告诉你钠会不会爆表。**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-麦当劳中国-red)](https://open.mcd.cn/mcp)

---

## 它解决什么

| 打工人真实痛点 | 本项目的做法 |
| --- | --- |
| 选择困难，每次都点一样的 | 直接给三套方案，各自写清取舍理由 |
| 想控体重但不知道该吃多少 | 按身高体重年龄算 BMR/TDEE，给出能量与三大营养素目标 |
| 「麦当劳不健康」是个模糊印象 | 拆成能量/蛋白质/脂肪/碳水/钠/钙六项逐项对比膳食指南 |
| 配好了还得自己打开 App 一个个点 | 方案自带餐品编码，可直接调 MCP 算价、领券、下单 |
| 营养建议全是套话 | 明确告诉你：**钠大概率会超 2000mg，膳食纤维一定不够** |

## 效果示例

> 175cm / 72kg / 28 岁男性，久坐办公，维持体重，深圳蛇口网谷餐厅（实时数据）

```
方案 A · 均衡日常                                        预估 ¥141.5
  早餐  原味板烧鸡腿麦满分 + 小杯玉米杯 + 热浓浓抹茶牛奶大杯    632 kcal
  午餐  麦香鱼 + 大薯条 + 大杯怡泉+C                         872 kcal
  晚餐  培根蔬萃双层牛堡 + 大杯玉米杯 + 可乐大杯               753 kcal
  合计  2257 kcal｜蛋白 81g｜脂肪 75g｜碳水 300g｜钠 2256mg｜钙 841mg

方案 B · 高蛋白保肌                                      预估 ¥142.5
  蛋白质 94g（目标抬到约 1.35 g/kg），代价是钠升到 2478mg

方案 C · 低钠轻负担                                      预估 ¥121.0
  钠压到 1984mg，代价是能量降到 1779 kcal、钙降到 658mg
```

同时会明确给出：钠超出上限 → 建议其他餐次多吃蔬果补钾、多喝水；
膳食纤维麦当劳本身吃不够 → 需要自行补充。

## 三套方案为什么不一样

不是随机扰动，是**三种明确的取舍**（预设会同时调整目标值与惩罚权重）：

| 预设 | 目标调整 | 权重调整 | 适合 |
| --- | --- | --- | --- |
| `balanced` 均衡日常 | — | 全面均衡 | 闭眼选这个 |
| `high_protein` 高蛋白保肌 | 蛋白质 ×1.35 | 蛋白质不足惩罚 ×3 | 训练日、减脂期 |
| `low_sodium` 低钠轻负担 | 钠上限 ×0.85 | 钠超标惩罚 ×9 | 体检钠偏高、今天别的两餐也咸 |

## 快速开始

### 1. 申请 MCP Token

到 [麦当劳 MCP 开放平台](https://open.mcd.cn/mcp) 用手机号登录 → 控制台 → 激活 → 复制 Token。

### 2. 配置

```bash
export MCD_MCP_TOKEN=<你的 Token>
```

（在 WorkBuddy 里用则参考 `mcp-config.example.json` 配置连接器，Token 只走环境变量。）

### 3. 跑

```bash
git clone https://github.com/<你的账号>/mcd-see-and-eat.git
cd mcd-see-and-eat

# 三餐都在麦当劳解决
python scripts/plan.py --sex male --age 28 --height 175 --weight 72 \
    --html today.html

# 只在麦当劳吃午餐，减脂，不喝可乐
python scripts/plan.py --sex female --age 30 --height 162 --weight 55 \
    --goal cut --meals lunch --exclude 可乐 --html today.html
```

无第三方依赖，Python 3.9 起可用，2–4 秒出结果。`--html` 生成的报告是单文件，手机打开也能看。

**没有 Token 也能体验**：加 `--offline`，用仓库 `data/` 下的真实快照（2026-10-16 采集）跑完整流程。

### 4. 下单（可选）

```bash
python scripts/order.py --plan today.json --meal lunch      # 查券 + 算价
python scripts/order.py --plan today.json --meal lunch --confirm   # 真下单（会扣款）
```

## 参数

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--sex` | male / female | male |
| `--age` | 年龄 | 28 |
| `--height` | 身高 cm | 172 |
| `--weight` | 体重 kg | 68 |
| `--activity` | sedentary / light / moderate / active | sedentary |
| `--goal` | cut / maintain / bulk | maintain |
| `--meals` | 哪几餐在麦当劳解决，逗号分隔 | breakfast,lunch,dinner |
| `--preset` | 只要某一套：balanced / high_protein / low_sodium | 全部三套 |
| `--exclude` | 忌口关键词 | — |
| `--dessert` | 允许加甜品 | 关 |
| `--store-code` / `--keyword` | 指定门店 / 搜门店 | 收藏门店第一家 |
| `--offline` | 用本地快照，不请求 MCP | 关 |
| `--json` / `--md` / `--html` | 输出路径 | 直接打印 Markdown |

## 它是怎么工作的

```
                 ┌──────────────── 麦当劳 MCP Server ────────────────┐
用户画像           │  query-nearby-stores   门店                      │
性别/年龄/身高/体重 │  now-time-info         服务器时间                 │     营养目标
活动量/目标    ──┐ │  list-nutrition-foods  160 条餐品营养             │  ┌──（膳食指南 2022 + DRIs 2023）
                │ │  query-meals  ×3       早/午/晚分时菜单 + 价格     │  │   能量 / 三大营养素 / 钠 / 钙
                │ └──────────────────────────────────────────────────┘  │
                │                        │                              │
                │                        ▼                              │
                │          双向对齐：营养表 × 在售菜单                    │
                │          剔除套餐与营养表未覆盖的新品，绝不估算           │
                │                        │                              │
                └────────────────────────┼──────────────────────────────┘
                                         ▼
                         束搜索：每餐「主食+小食+饮品」穷举 → 保留 Top-56
                                 三餐再组合 → 按整日目标 + 供能比 + 重复度打分
                                         ▼
                               三套方案 + Markdown / HTML 报告
```

关键点：

- **分时菜单**：麦当劳早餐只供应到约 10:30，直接查「当前菜单」配不出早餐。
  本项目按早/午/晚分别用 `query-meals` 的 `reservationDate` 拉对应时段菜单。
- **以营养表为规划单位**：`可乐中杯/大杯`、`薯条小/中/大` 营养不同，
  营养表条目规格最精确，再反查菜单拿价格与编码，保证「配得出来就点得到」。
- **不猜数**：营养表里查不到的一律剔除，报告里明确列出被剔除的原因。

## 目标用户

- 工作日靠麦当劳解决一顿或几顿、又在意体重和健康的城市打工人
- 健身/减脂人群：需要精确控制蛋白质和能量，但不想顿顿自己做饭
- 需要控钠但又离不开快餐的人（本项目会如实告诉你哪里压不住）
- 把「今天吃什么」当成决策负担的人

## 目录结构

```
SKILL.md                 WorkBuddy Skill 描述（对话中直接调用）
scripts/
  plan.py                主入口：配餐 + 出报告
  order.py               查券 → 算价 → 下单
  refresh_menu.py        刷新 data/ 快照
src/seeandeat/
  mcp_client.py          麦当劳 MCP Streamable HTTP 客户端（零第三方依赖）
  dri.py                 营养目标（膳食指南 + DRIs）
  menu.py                营养表 × 在售菜单双向对齐
  taxonomy.py            餐品角色分类与餐次适配
  planner.py             束搜索组合优化
  report.py              Markdown / HTML 报告
  pipeline.py            端到端编排
data/                    2026-10-16 采集的真实快照（离线演示用）
references/dietary_guidelines.md   每个目标数字的出处
MCP_INTEGRATION.md       用了哪些 MCP Tool、怎么调用、业务价值
```

## 作为 WorkBuddy Skill 使用

把本仓库放到 `~/.workbuddy/skills/mcd-see-and-eat/` 后，直接在对话里说：

> 「我 175、72 公斤，久坐，帮我配今天的麦当劳三餐」

Skill 会先向你要齐身高体重等信息，再跑完整流程并把 HTML 报告推给你。

## 免责声明

本项目为麦当劳程序员节创意开发大赛参赛作品，**非麦当劳官方产品**。
输出仅供参考，不构成医疗、营养或专业建议；餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。
有高血压、肾病、糖尿病等基础疾病者请遵医嘱。

© 2026 McDonald's. All Rights Reserved. 麦当劳相关商标归其权利人所有。

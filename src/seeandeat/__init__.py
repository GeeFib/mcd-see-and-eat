"""看见什么吃什么 —— 用麦当劳 MCP 的在售餐品与营养数据配一日三餐。

核心模块：
- mcp_client：麦当劳 MCP Server 的 Streamable HTTP 客户端（零依赖）
- dri       ：按《中国居民膳食指南（2022）》与 DRIs（2023）算每日营养目标
- menu      ：营养表 × 在售菜单双向对齐，合成可点餐品池
- taxonomy  ：餐品角色分类与餐次适配
- planner   ：束搜索组合优化，产出三套侧重不同的方案
- report    ：Markdown / HTML 报告
- pipeline  ：端到端编排
"""

__version__ = "1.0.0"

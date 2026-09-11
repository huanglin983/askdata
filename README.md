# 指标配置管理 · 规则引擎 Text-to-SQL Demo

Python + Flask + SQLite。语义层元数据驱动，AI/意图层只出结构化意图，规则引擎生成并执行 SQL。

## 文档（拆库带走）

| 文档 | 内容 |
|---|---|
| [doc/README.md](doc/README.md) | 文档索引 |
| [doc/01_技术方案.md](doc/01_技术方案.md) | 架构与问数链路 |
| [doc/02_计算与币种规则.md](doc/02_计算与币种规则.md) | 五阶段汇率、币种、复合验算 |
| [doc/03_元数据与配置模型.md](doc/03_元数据与配置模型.md) | 表结构与绑定模型 |
| [doc/04_Demo实现与拆库说明.md](doc/04_Demo实现与拆库说明.md) | 代码映射与独立建库步骤 |
| [doc/05_详细设计_Flask与实现.md](doc/05_详细设计_Flask与实现.md) | Flask/Web 入门与工程详细设计 |

## 能力

- **指标管理**统一入口（`/metrics`）：原子 / 派生 / 复合三类卡片 + 列表 Tab；紧凑表省略展示
- 业务架构主数据（业务线 → 主题域 → 业务对象 → 业务过程）
- 表/字段元数据 + 事实↔维度关系；分析维度来自字段勾选（`is_analysis_dim`）+ 指标绑定
- 原子：绑定物理字段、尽量少写过滤；自动/手工 SQL；阶段汇率字段在原子层维护
- 派生：原子 + 业务切片；金额/汇率/分类继承自原子
- 复合：同粒度四则运算（如 `(A+B)/C`）；粒度/币种校验
- 指标地图：复合向下展开依赖树
- 规则引擎：阶段汇率绑定、币种注入、粒度/维度校验、JOIN、复合先换算再运算
- 问数：结构化表单，或自然语言（阿里云百炼 Intent → 失败回退关键词）→ SQL → 样例执行 → 中文审计

## 启动

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\pip install -r requirements.txt
# 配置百炼（可选）：复制 .env.example 为 .env，填写 DASHSCOPE_API_KEY / BASE_URL
.\.venv\Scripts\python app.py

# macOS / Linux
# .venv/bin/pip install -r requirements.txt
# .venv/bin/python app.py
```

浏览器：http://127.0.0.1:5050  

首次启动自动建库灌种（`data/demo.db`，已 gitignore）。

### 百炼意图（自然语言模式）

| 环境变量 | 说明 |
|---|---|
| `DASHSCOPE_API_KEY` | 百炼 API Key（未配置则只用关键词） |
| `DASHSCOPE_BASE_URL` | OpenAI 兼容地址，如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
| `DASHSCOPE_MODEL` | 默认 `qwen-plus` |
| `INTENT_PROVIDER` | `auto`（默认）/ `bailian` / `keyword` |

密钥放在本地 `.env`（已 gitignore），勿提交仓库。模型只输出 Intent JSON，SQL 仍由规则引擎生成。

## 推荐演示

1. **问数 Demo** → 选「自然语言」→ 问句如「查询项目P001的成本GAP，人民币，带电站」
2. 意图来源应显示「阿里云百炼」（已配 Key）或「关键词」
3. 核对 SQL：各阶段先乘本阶段汇率再相减；期望 GAP **1540**（详见 [doc/02](doc/02_计算与币种规则.md)）

## 目录

```text
app.py / db.py / engine.py / intent.py / intent_llm.py / display.py
meta.py / biz_arch.py / metric_sql.py / metric_map.py
templates/  static/  data/  doc/  requirements.txt  .env.example
```

## 边界

不接生产数仓；公式为子指标名 + 四则运算；无登录权限。扩展方式见 [doc/04](doc/04_Demo实现与拆库说明.md)。

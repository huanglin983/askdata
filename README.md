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
- 问数：表单或关键词意图 → SQL → 样例执行 → 中文审计展示

## 启动

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python app.py

# macOS / Linux
# .venv/bin/pip install -r requirements.txt
# .venv/bin/python app.py
```

浏览器：http://127.0.0.1:5050  

首次启动自动建库灌种（`data/demo.db`，已 gitignore）。

## 推荐演示

1. **问数 Demo** → 指标「成本GAP」、币种 CNY、维度「电站」、项目 `P001`
2. 核对 SQL：各阶段先乘本阶段汇率再相减
3. 期望：Contract 8640 − PJ 7100 = **GAP 1540**（详见 [doc/02](doc/02_计算与币种规则.md)）

## 目录

```text
app.py / db.py / engine.py / intent.py / display.py
meta.py / biz_arch.py / metric_sql.py / metric_map.py
templates/  static/  data/  doc/  requirements.txt
```

## 边界

不接真实大模型与生产仓；公式为子指标名 + 四则运算；无登录权限。扩展方式见 [doc/04](doc/04_Demo实现与拆库说明.md)。

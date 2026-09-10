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

## 能力

- 原子 / 派生 / 复合指标 Web CRUD
- 分析维度主数据 + 指标绑定
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
templates/  static/  data/  doc/  requirements.txt
```

## 边界

不接真实大模型与生产仓；公式为子指标名 + 四则运算；无登录权限。扩展方式见 [doc/04](doc/04_Demo实现与拆库说明.md)。

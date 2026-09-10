# 04 · Demo 实现与拆库说明

本文说明当前代码如何落地方案，以及如何把本目录提升为**独立代码库**。

## 1. 代码 ↔ 方案映射

| 方案能力 | 代码文件 |
|---|---|
| SQLite schema / 种子 / CRUD | `db.py` |
| 规则引擎（汇率、币种、复合、校验、JOIN） | `engine.py` |
| 意图解析（表单 + 关键词） | `intent.py` |
| 中文展示（意图/审计/列名） | `display.py` |
| Web 管理与问数 | `app.py` + `templates/` |
| 方案与计算文档 | `doc/` |

## 2. 推荐仓库目录（拆库后根目录）

将当前 `metric_t2sql_demo/` **整体**作为新仓库根即可：

```text
.
├── README.md                 # 启动与能力说明
├── requirements.txt
├── .gitignore
├── app.py
├── db.py
├── engine.py
├── intent.py
├── display.py
├── static/
├── templates/
├── data/                     # demo.db 运行时生成，勿提交
└── doc/                      # 本目录：方案 + 计算 + 元数据
    ├── README.md
    ├── 01_技术方案.md
    ├── 02_计算与币种规则.md
    ├── 03_元数据与配置模型.md
    └── 04_Demo实现与拆库说明.md
```

## 3. 拆库步骤（建议）

1. 复制或 `git subtree` / 新建空库后拷贝上述文件（**不要**拷贝 `.venv/`、`data/*.db`）。
2. 确认根 `README.md` 仅引用 `doc/`，不依赖原「支架项目」路径。
3. `python -m venv .venv && pip install -r requirements.txt && python app.py`。
4. 浏览器打开 `http://127.0.0.1:5050`，按 `doc/02` 样例验算 GAP=1540。
5. （可选）删改种子数据表名/字段以对接新业务，但保留元数据表结构与引擎约束。

### 快速初始化命令

```bash
# 在新仓库根目录
python -m venv .venv

# Windows
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python app.py

# Linux / macOS
# .venv/bin/pip install -r requirements.txt
# .venv/bin/python app.py
```

## 4. 明确不在 Demo / 首版库内的范围

- 真实大模型 API（意图层可后续替换 `intent.py`）
- MaxCompute / 生产数仓直连（替换执行层即可）
- 登录权限、审批流、Excel 批量导入
- 通用公式 AST（当前为子指标名 + 四则运算）

## 5. 扩展建议（拆库后）

| 方向 | 建议 |
|---|---|
| 生产执行 | `engine._execute` 改为 JDBC/ODPS 客户端，SQL 方言保持引擎生成 |
| 真 NLP | 意图输出对齐 `Intent` 数据结构，勿让模型写 SQL |
| 配置同步 | 从指标平台 API/Excel 导入到 `metric_*` 表 |
| 多事实表 | 扩展 `source_table` 路由与 JOIN 注册表 |

## 6. 与原项目关系

本 Demo 最初位于支架项目旁路目录；**方案与实现已自包含于本树**。拆库后以 `doc/01`～`03` 为规范基线，`doc/04` 为工程说明；原仓库中的 `指标体系与txttosql.md` 可作为历史稿，以本 `doc/` 为准。

# 04 · Demo 实现与拆库说明

本文说明当前代码如何落地方案，以及如何把本目录提升为**独立代码库**。

## 1. 代码 ↔ 方案映射

| 方案能力 | 代码文件 |
|---|---|
| SQLite schema / 种子 / 指标 CRUD / 维度绑定 | `db.py` |
| 数仓表/字段/关系元数据 | `meta.py` |
| 业务架构（线/域/对象/过程） | `biz_arch.py` |
| 原子/派生 SQL 预览与过滤安全 | `metric_sql.py` |
| 规则引擎（汇率、币种、复合、校验、JOIN） | `engine.py` |
| 指标依赖地图（业务架构根 + 侧栏子树） | `metric_map.py` |
| 意图解析（表单 + ChatBI 流水线 / 百炼 / 关键词） | `intent.py` / `intent_chatbi.py` / `intent_llm.py` / `chatbi/` |
| 中文展示（意图/审计/列名） | `display.py` |
| Web 管理与问数 | `app.py` + `templates/` + `static/style.css` |
| 方案与计算文档 | `doc/` |

## 2. Web 模块一览

| 模块 | 入口 | 说明 |
|---|---|---|
| 指标管理 | `/metrics` | 三类指标卡片（矢量图标 + 帮助文案 + 数量）；进入列表后用 Tab 切换 |
| 原子 / 派生 / 复合 | `/metrics/{atomic\|derived\|composite}` | CRUD、SQL 预览、关键词 `?q=`、紧凑表；原子→创建派生、派生→创建复合快捷链 |
| 指标地图 | `/metrics/map` | 业务线→主题域→业务对象→业务过程为根，挂载复合/派生并展开到原子；孤立原子；`?panel=` 侧栏（原子反向引用） |
| 业务架构 | `/meta/biz-arch` | 四级分类树维护（地图骨架来源） |
| 元数据 / 维度关系 | `/meta/tables`、`/meta/rels` | 表字段与 JOIN |
| 问数 Demo | `/ask` | 表单或自然语言（ChatBI 主链路：Mapper+LLM/规则+校验消歧；失败回退百炼/关键词）→ 引擎执行或口径/字典 |

顶栏**不再**并列「原子指标 / 派生指标 / 复合指标」三个入口，统一为「指标管理」。

## 3. 推荐仓库目录（拆库后根目录）

将当前工程**整体**作为新仓库根即可：

```text
.
├── README.md                 # 启动与能力说明
├── requirements.txt
├── .gitignore
├── app.py
├── db.py
├── meta.py
├── biz_arch.py
├── metric_sql.py
├── metric_map.py
├── engine.py
├── intent.py
├── intent_llm.py
├── display.py
├── static/
│   └── style.css             # 含 metric-hub / metric-tabs / table.compact
├── templates/
│   ├── base.html             # 顶栏导航
│   ├── metrics.html          # 指标管理总览
│   ├── _metric_tabs.html     # 列表页类型 Tab
│   ├── atomic_*.html / derived_*.html / composite_*.html
│   ├── meta_*.html / biz_arch*.html / ask.html
│   ├── metric_map.html / metric_map_panel.html / _metric_map_macros.html
│   └── _filters.html / _dim_bind.html
├── data/                     # demo.db 运行时生成，勿提交
└── doc/                      # 本目录：方案 + 计算 + 元数据 + 需求规划
    ├── README.md
    ├── 01_技术方案.md
    ├── 02_计算与币种规则.md
    ├── 03_元数据与配置模型.md
    ├── 04_Demo实现与拆库说明.md
    ├── 05_详细设计_Flask与实现.md
    ├── 06_需求规划一_Dify与问数查询服务.md
    ├── 07_指标分层_理论配置与SQL生成.md
    └── diagrams/             # 交互流程图等
```

## 4. 拆库步骤（建议）

1. 复制或 `git subtree` / 新建空库后拷贝上述文件（**不要**拷贝 `.venv/`、`data/*.db`、`__pycache__/`）。
2. 确认根 `README.md` 仅引用 `doc/`，不依赖原旁路项目路径。
3. `python -m venv .venv && pip install -r requirements.txt && python app.py`。
4. 浏览器打开 `http://127.0.0.1:5050`：
   - 顶栏进入 **指标管理**，核对三类卡片、列表关键词与「创建派生 / 创建复合」；
   - **指标地图**确认业务架构为根、侧栏可打开；
   - **问数 Demo** 按 `doc/02` 验算 GAP=1540。
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

> 开发时若关闭 Flask reloader（`use_reloader=False`），改路由后需**手动重启**进程，否则会出现 `BuildError: metrics_hub` 一类旧进程未加载新端点的问题。

## 5. 明确不在 Demo / 首版库内的范围

- 向量召回 ANN（`EmbeddingMapper` 仅占位）
- 外部指标血缘服务（ChatBI `_get_metric_lineage` 占位；现网血缘仍走 `intent_llm.lineage_related_ids`）
- MaxCompute / 生产数仓直连（替换执行层即可）
- 登录权限、审批流、Excel 批量导入
- 通用公式 AST（当前为子指标名 + 四则运算）
- 独立的「分析维度」CRUD 页（已并入表字段 `is_analysis_dim`）

> 自然语言意图：**已支持** ChatBI 流水线（词典 Mapper + 百炼/规则兜底 + 校验消歧）；无 Key 走规则兜底；ChatBI 失败可回退旧百炼/关键词。细节见 [05 §10](05_详细设计_Flask与实现.md)。

## 6. 扩展建议（拆库后）

| 方向 | 建议 |
|---|---|
| 生产执行 | `engine._execute` 改为 JDBC/ODPS 客户端，SQL 方言保持引擎生成 |
| 真 NLP | 继续扩展 `chatbi/`；输出对齐 `Intent` / `IntentStruct`，勿让模型写 SQL |
| 向量召回 | 实现 `EmbeddingMapper` + 白名单校验，禁止模糊编造指标名 |
| 配置同步 | 从指标平台 API/Excel 导入到 `metric_*` 表 |
| 多事实表 | 扩展 `source_table` 路由与 `meta_table_rel` |

## 7. 与原项目关系

本 Demo 最初位于支架项目旁路目录；**方案与实现已自包含于本树**。拆库后以 `doc/01`～`03` 为规范基线，`doc/04` 为工程说明；历史旁路稿以本 `doc/` 为准。

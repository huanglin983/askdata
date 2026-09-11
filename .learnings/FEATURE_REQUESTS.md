# Feature Requests

Missing capabilities requested by users, captured during development.

**Areas**: frontend | backend | infra | tests | docs | config
**Statuses**: pending | in_progress | resolved | wont_fix
**Complexity**: simple | medium | complex

## Status Definitions

| Status | Meaning |
|--------|---------|
| `pending` | Not yet addressed |
| `in_progress` | Actively being built |
| `resolved` | Capability implemented (add Resolution block) |
| `wont_fix` | Decided not to build (reason in Resolution) |

Entry format: see the self-improvement skill's "Feature Request Entry" section. IDs use `FEAT-YYYYMMDD-XXX`.

---

## [FEAT-20260911-001] Atomic filters + SQL rewrite; derived filters; FX on atomic

**Logged**: 2026-09-11T08:35:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend
**Complexity**: medium

### Requested Capability
1. Atomic metrics support filter conditions, auto-generated SQL, and manual SQL rewrite.
2. Derived metrics add filters on top of atomic; stage FX (amount/rate) maintenance moves to atomic layer.

### Use Case
Configure metric-level WHERE at atomic/derived layers; keep stage exchange-rate pairing with amount atomics so derived only composes currency + extra filters.

### Suggested Approach
- Extend `metric_atomic` with `filter_json`, `sql_expr`, `sql_manual`, `stage_type`, `rate_col`
- Extend `metric_derived` with `filter_json`; inherit FX from atomic at upsert/resolve
- Engine: CASE WHEN filters + currency template from atomic fields

### Resolution
**Resolved**: 2026-09-11
**Resolution**: Implemented in `metric_sql.py`, `db.py`, `engine.py`, `app.py`, templates, and `doc/03`. Verified cost GAP=1540 and derived filter stacking.

### Metadata
**Tags**: metrics, sql, filters, fx
**See Also**: 

---

## [FEAT-20260911-002] Metric dependency map (org-chart)

**Logged**: 2026-09-11T09:16:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: frontend
**Complexity**: medium

### Requested Capability
指标地图：从复合指标向下展开到派生/原子，组织架构图展示依赖树；无引用指标单独罗列「孤立指标」；卡片样式含类型角标与影响因子。

### Use Case
一眼看清复合→派生→原子依赖，以及未被引用的孤立指标。

### Suggested Approach
- Build adjacency from `metric_composite.sub_metric_ids` and `metric_derived.atomic_id`
- Root = composites not referenced by other composites
- Orphans = metrics not reachable from any root tree
- CSS org-chart cards (no heavy graph lib)

### Resolution
**Resolved**: 2026-09-11
**Resolution**: Added `metric_map.py`, `/metrics/map`, `templates/metric_map.html`, nav link, and map CSS.

### Metadata
**Tags**: metrics, map, lineage, ui
**See Also**: FEAT-20260911-001

## [FEAT-20260911-003] Metric tree zoom + collapse

**Logged**: 2026-09-11T10:07:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: frontend
**Complexity**: simple

### Requested Capability
指标树画布支持缩小/放大/最佳大小；指标树节点支持折叠与展开（默认展开）。

### Use Case
大依赖树可缩放到可视区域，并按需收起分支以聚焦上层结构。

### Suggested Approach
- CSS transform scale + viewport shell for scrollbars
- Per-node toggle on cards with children; `.is-collapsed` hides child `ul`

### Resolution
**Resolved**: 2026-09-11
**Resolution**: Updated `templates/metric_map.html` and `static/style.css` with zoom toolbar and collapse toggles.

### Metadata
**Tags**: metrics, map, zoom, collapse, ui
**See Also**: FEAT-20260911-002

## [FEAT-20260911-003] Table grain / business primary key

**Logged**: 2026-09-11T10:10:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend
**Complexity**: medium

### Requested Capability
元数据支持表的数据粒度配置（即业务主键）。

### User Context
多维模型需要在表级明确业务主键，与物理 PK 区分，并驱动字段 grain 语义与问数默认粒度列。

### Complexity Estimate
medium

### Suggested Implementation
- `meta_table.grain_keys` JSON 有序字段名
- 表编辑页勾选业务主键；同步 `semantic_role=grain`
- `get_fact_grain_field()` 取首个 grain key

### Resolution
**Resolved**: 2026-09-11
**Resolution**: Implemented in `meta.py` (schema/migrate/helpers), `app.py` table edit, `templates/meta_table_*.html`, and `doc/03`.

### Metadata
**Tags**: metadata, grain, business-key, meta_table

## [FEAT-20260911-004] Business architecture catalog

**Logged**: 2026-09-11T10:25:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend
**Complexity**: medium

### Requested Capability
元数据增加业务架构维护：业务线=支架、主题域=财务、业务对象=项目、业务过程按阶段；参考目录树+列表实现图。

### User Context
指标分类需有可维护的四级主数据，供原子/派生/复合引用。

### Resolution
**Resolved**: 2026-09-11
**Resolution**: Added `biz_arch.py`, `/meta/biz-arch` UI (tree+table), seed hierarchy, cascading selects on atomic metrics.

### Metadata
**Tags**: biz-arch, metadata, taxonomy

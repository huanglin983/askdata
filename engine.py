"""Rule engine: metadata-driven SQL generation + execute + audit."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import db
import meta
import metric_sql


FACT_ALIAS = "m"
FACT_TABLE_DEFAULT = "ads_fin_tracker_comparison_summary_df"


@dataclass
class Intent:
    metric_ids: list[str] = field(default_factory=list)  # derived / composite / atomic ids
    currency: str = "CNY"
    dims: list[str] = field(default_factory=list)  # e.g. power_plant, region
    filters: dict[str, str] = field(default_factory=dict)  # project_number=
    raw_text: str = ""
    source: str = ""  # form | keyword | bailian | chatbi
    intent_type: str = "数据查询"  # 数据查询|指标口径咨询|指标字典检索
    include_related: bool = False
    calc_instruction: str | None = None
    metric_names: list[str] = field(default_factory=list)
    related_metric_ids: list[str] = field(default_factory=list)
    payload_zh: dict[str, Any] = field(default_factory=dict)
    disambiguate_msg: str | None = None  # ChatBI 多指标消歧反问


@dataclass
class EngineResult:
    ok: bool
    sql: str = ""
    intent: dict[str, Any] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    error: str = ""


def _fact_table_name() -> str:
    fact = meta.get_primary_fact_table()
    return fact["physical_name"] if fact else FACT_TABLE_DEFAULT


def _grain_field() -> str:
    """Primary grain column from fact meta, else project_number."""
    return meta.get_fact_grain_field()


def run(intent: Intent) -> EngineResult:
    """Full pipeline: load meta -> bind -> currency -> check -> join -> SQL -> execute."""
    try:
        from intent_llm import intent_to_engine_dict

        intent_dict = intent_to_engine_dict(intent)
    except Exception:  # noqa: BLE001
        intent_dict = {
            "metric_ids": intent.metric_ids,
            "currency": intent.currency,
            "dims": intent.dims,
            "filters": intent.filters,
            "raw_text": intent.raw_text,
            "source": intent.source or "",
            "intent_type": getattr(intent, "intent_type", "数据查询"),
            "include_related": getattr(intent, "include_related", False),
            "calc_instruction": getattr(intent, "calc_instruction", None),
            "metric_names": list(getattr(intent, "metric_names", []) or []),
            "related_metric_ids": list(getattr(intent, "related_metric_ids", []) or []),
            "payload_zh": dict(getattr(intent, "payload_zh", {}) or {}),
        }
    if not intent.metric_ids:
        return EngineResult(ok=False, intent=intent_dict, error="未指定指标")

    currency = (intent.currency or "CNY").upper()
    rule = db.get_currency_rule(currency)
    if not rule:
        return EngineResult(
            ok=False, intent=intent_dict, error=f"不支持的币种: {currency}"
        )

    try:
        fact_table = _fact_table_name()
        grain_col = _grain_field()

        # validate requested analysis dims against meta fields + metric binds
        dim_defs: list[dict] = []
        for code in intent.dims:
            d = meta.get_analysis_field_by_code(code)
            if not d or not d.get("enabled"):
                return EngineResult(
                    ok=False,
                    intent=intent_dict,
                    error=f"分析维度未在元数据中启用: {code}",
                )
            if d.get("dim_role") == "grain" or d.get("semantic_role") == "grain":
                return EngineResult(
                    ok=False,
                    intent=intent_dict,
                    error=f"{code} 是粒度维度，已默认输出，请勿作为附加分析维度勾选",
                )
            dim_defs.append(d)

        allowed_sets: list[set[str]] = []
        for mid in intent.metric_ids:
            mtype = db.resolve_metric_type(mid)
            if not mtype or mtype == "atomic":
                row = db.get_derived(mid) or db.get_derived_by_name(mid)
                if row:
                    mtype, mid = "derived", row["id"]
                else:
                    row = db.get_composite(mid) or db.get_composite_by_name(mid)
                    if row:
                        mtype, mid = "composite", row["id"]
                    else:
                        return EngineResult(
                            ok=False, intent=intent_dict, error=f"指标不存在: {mid}"
                        )
            allowed = set(db.get_metric_analysis_codes(mtype, mid))
            allowed_sets.append(allowed)

        if dim_defs and allowed_sets:
            common = set.intersection(*allowed_sets) if allowed_sets else set()
            for d in dim_defs:
                if d["code"] not in common:
                    return EngineResult(
                        ok=False,
                        intent=intent_dict,
                        error=(
                            f"维度「{d['name']}」未同时绑定到所选指标，"
                            f"允许交叉: {sorted(common) or '无'}"
                        ),
                    )

        select_parts: list[str] = [f"{FACT_ALIAS}.{grain_col} AS {grain_col}"]
        audit: list[dict[str, Any]] = []
        grains: list[set[str]] = []
        needed_tables: set[str] = set()

        for mid in intent.metric_ids:
            expr, meta_audit, grain = _resolve_metric(mid, currency, rule["expr_template"])
            alias = _safe_alias(meta_audit["name"], mid)
            select_parts.append(f"({expr}) AS {alias}")
            audit.append(meta_audit)
            grains.append(grain)

        if len(grains) > 1:
            base = grains[0]
            for g in grains[1:]:
                if g != base:
                    return EngineResult(
                        ok=False,
                        intent=intent_dict,
                        audit=audit,
                        error=f"粒度不一致: {base} vs {g}",
                    )

        # map physical table -> alias for SELECT/WHERE
        table_alias: dict[str, str] = {fact_table: FACT_ALIAS}

        for d in dim_defs:
            field = d["source_field"]
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", field):
                return EngineResult(
                    ok=False, intent=intent_dict, error=f"非法维度字段: {field}"
                )
            src = d["source_table"]
            if src == fact_table:
                select_parts.append(f"{FACT_ALIAS}.{field} AS {d['code']}")
            else:
                needed_tables.add(src)
                al = table_alias.setdefault(src, meta.dim_alias_for(src))
                select_parts.append(f"{al}.{field} AS {d['code']}")

        wheres: list[str] = []
        params: list[Any] = []
        for k, v in (intent.filters or {}).items():
            if not v:
                continue
            if k == grain_col or k == "project_number":
                wheres.append(f"{FACT_ALIAS}.{grain_col} = ?")
                params.append(v)
                continue
            ddef = meta.get_analysis_field_by_code(k)
            if not ddef:
                continue
            src = ddef["source_table"]
            if src == fact_table:
                wheres.append(f"{FACT_ALIAS}.{ddef['source_field']} = ?")
            else:
                needed_tables.add(src)
                al = table_alias.setdefault(src, meta.dim_alias_for(src))
                wheres.append(f"{al}.{ddef['source_field']} = ?")
            params.append(v)

        joins = meta.resolve_joins_for_tables(needed_tables)
        # fallback: legacy single dim_project join if meta empty but needed
        if needed_tables and not joins:
            for t in needed_tables:
                joins.append(
                    {
                        "dim_physical": t,
                        "fact_physical": fact_table,
                        "join_type": "LEFT",
                        "join_keys_list": [
                            {"left": grain_col, "right": grain_col}
                        ],
                    }
                )

        sql = (
            f"SELECT\n  "
            + ",\n  ".join(select_parts)
            + f"\nFROM {fact_table} {FACT_ALIAS}"
        )
        joined: set[str] = set()
        for j in joins:
            dim_phys = j["dim_physical"]
            if dim_phys in joined:
                continue
            al = table_alias.setdefault(dim_phys, meta.dim_alias_for(dim_phys))
            jtype = (j.get("join_type") or "LEFT").upper()
            if jtype not in ("LEFT", "INNER", "RIGHT"):
                jtype = "LEFT"
            ons = []
            for pair in j.get("join_keys_list") or []:
                ons.append(
                    f"{FACT_ALIAS}.{pair['left']} = {al}.{pair['right']}"
                )
            if not ons:
                continue
            sql += f"\n{jtype} JOIN {dim_phys} {al}\n  ON " + " AND ".join(ons)
            joined.add(dim_phys)

        missing = needed_tables - joined
        if missing:
            return EngineResult(
                ok=False,
                intent=intent_dict,
                audit=audit,
                error=f"缺少事实表到维度表的关系配置: {sorted(missing)}",
            )

        if wheres:
            sql += "\nWHERE " + " AND ".join(wheres)
        sql += f"\nORDER BY {FACT_ALIAS}.{grain_col}"

        columns, rows = _execute(sql, params)
        return EngineResult(
            ok=True,
            sql=sql,
            intent=intent_dict,
            audit=audit,
            columns=columns,
            rows=rows,
        )
    except Exception as e:  # noqa: BLE001 — demo surface errors to UI
        return EngineResult(ok=False, intent=intent_dict, error=str(e))


def _resolve_metric(
    metric_id: str, currency: str, expr_template: str
) -> tuple[str, dict[str, Any], set[str]]:
    """Return (sql_expr, audit, grain_set). Supports atomic / derived / composite."""
    derived = db.get_derived(metric_id)
    if derived:
        return _resolve_derived(derived, currency, expr_template)

    composite = db.get_composite(metric_id)
    if composite:
        return _resolve_composite(composite, currency, expr_template)

    atomic = db.get_atomic(metric_id)
    if atomic:
        return _resolve_atomic(atomic, currency, expr_template)

    # allow resolve by name as fallback
    derived = db.get_derived_by_name(metric_id)
    if derived:
        return _resolve_derived(derived, currency, expr_template)
    composite = db.get_composite_by_name(metric_id)
    if composite:
        return _resolve_composite(composite, currency, expr_template)
    atomic = db.get_atomic_by_name(metric_id)
    if atomic:
        return _resolve_atomic(atomic, currency, expr_template)

    raise ValueError(f"指标不存在: {metric_id}")


def _resolve_atomic(
    atomic: sqlite3.Row, currency: str, expr_template: str
) -> tuple[str, dict[str, Any], set[str]]:
    """Resolve an atomic metric directly (currency-converted when rate_col present)."""
    amount_col = atomic["source_field"]
    rate_col = atomic["rate_col"] if "rate_col" in atomic.keys() else None
    stage_type = atomic["stage_type"] if "stage_type" in atomic.keys() else None
    atomic_filters = metric_sql.row_filter_text(atomic)
    sql_manual = bool(atomic["sql_manual"]) if "sql_manual" in atomic.keys() else False

    if not amount_col:
        raise ValueError(f"原子指标未维护来源字段: {atomic['name']}")

    # Amount atomics with FX: apply currency template (same as derived).
    # Rate-only / no-FX atomics: raw amount expression (ORIGIN-style).
    if rate_col:
        if not _same_stage_prefix(amount_col, rate_col) and currency != "ORIGIN":
            raise ValueError(
                f"跨阶段汇率混用禁止: {amount_col} vs {rate_col} ({atomic['name']})"
            )
        if sql_manual and atomic["sql_expr"]:
            manual = (atomic["sql_expr"] or "").strip()
            if manual.upper().startswith("SELECT"):
                amount_expr = metric_sql.build_atomic_amount_expr(
                    amount_col, atomic_filters, sql_manual=False
                )
            else:
                amount_expr = manual
        else:
            amount_expr = metric_sql.build_atomic_amount_expr(
                amount_col, atomic_filters, sql_manual=False
            )
        expr = expr_template.format(
            amount=amount_expr, rate=f"{FACT_ALIAS}.{rate_col}"
        )
    else:
        if currency not in ("ORIGIN",) and stage_type:
            raise ValueError(
                f"原子指标「{atomic['name']}」未维护同阶段汇率，无法换算为 {currency}"
            )
        if sql_manual and atomic["sql_expr"]:
            manual = (atomic["sql_expr"] or "").strip()
            if manual.upper().startswith("SELECT"):
                expr = metric_sql.build_atomic_amount_expr(
                    amount_col, atomic_filters, sql_manual=False
                )
            else:
                expr = manual
        else:
            expr = metric_sql.build_atomic_amount_expr(
                amount_col, atomic_filters, sql_manual=False
            )

    grain = set(db.get_metric_grain_codes("atomic", atomic["id"]))
    analysis = db.get_metric_analysis_codes("atomic", atomic["id"])
    audit = {
        "type": "atomic",
        "id": atomic["id"],
        "name": atomic["name"],
        "stage_type": stage_type,
        "amount_col": amount_col,
        "rate_col": rate_col,
        "atomic_filters": atomic_filters,
        "sql_manual": sql_manual,
        "currency": currency,
        "expr": expr,
        "grain": sorted(grain),
        "analysis_dims": analysis,
    }
    return expr, audit, grain


def _resolve_derived(
    row: sqlite3.Row, currency: str, expr_template: str
) -> tuple[str, dict[str, Any], set[str]]:
    atomic = db.get_atomic(row["atomic_id"])
    if not atomic:
        raise ValueError(f"派生指标缺少来源原子: {row['name']} -> {row['atomic_id']}")

    # FX pair maintained on atomic layer; derived may denormalize for listing
    amount_col = atomic["source_field"] or row["amount_col"]
    rate_col = (atomic["rate_col"] if atomic["rate_col"] else None) or row["rate_col"]
    stage_type = (atomic["stage_type"] if atomic["stage_type"] else None) or row["stage_type"]

    if not amount_col or not rate_col:
        raise ValueError(
            f"原子指标未维护金额/汇率字段: {atomic['name']} "
            f"(source_field={amount_col!r}, rate_col={rate_col!r})"
        )

    # hard rule: same-stage prefix binding
    if not _same_stage_prefix(amount_col, rate_col) and currency != "ORIGIN":
        raise ValueError(
            f"跨阶段汇率混用禁止: {amount_col} vs {rate_col} ({row['name']})"
        )

    atomic_filters = metric_sql.row_filter_text(atomic)
    derived_filters = metric_sql.row_filter_text(row)
    sql_manual = bool(atomic["sql_manual"]) if "sql_manual" in atomic.keys() else False

    # Manual rewrite: full SELECT is documentation; non-SELECT fragment used as amount expr.
    if sql_manual and atomic["sql_expr"]:
        manual = (atomic["sql_expr"] or "").strip()
        if manual.upper().startswith("SELECT"):
            amount_expr = metric_sql.build_atomic_amount_expr(
                amount_col, atomic_filters, sql_manual=False
            )
        else:
            amount_expr = manual
    else:
        amount_expr = metric_sql.build_atomic_amount_expr(
            amount_col, atomic_filters, sql_manual=False
        )

    # Derived filters stack on top of atomic
    if derived_filters:
        amount_expr = metric_sql.wrap_case(f"({amount_expr})", derived_filters)

    amount_ref = amount_expr
    rate_ref = f"{FACT_ALIAS}.{rate_col}"
    expr = expr_template.format(amount=amount_ref, rate=rate_ref)

    grain = set(db.get_metric_grain_codes("derived", row["id"]))
    analysis = db.get_metric_analysis_codes("derived", row["id"])
    audit = {
        "type": "derived",
        "id": row["id"],
        "name": row["name"],
        "stage_type": stage_type,
        "atomic_id": atomic["id"],
        "atomic_name": atomic["name"],
        "amount_col": amount_col,
        "rate_col": rate_col,
        "atomic_filters": atomic_filters,
        "derived_filters": derived_filters,
        "sql_manual": sql_manual,
        "currency": currency,
        "expr": expr,
        "grain": sorted(grain),
        "analysis_dims": analysis,
    }
    return expr, audit, grain


def _resolve_composite(
    row: sqlite3.Row, currency: str, expr_template: str
) -> tuple[str, dict[str, Any], set[str]]:
    """Expand formula: each child independently currency-converted, then arithmetic."""
    formula = row["formula"]
    sub_ids = json.loads(row["sub_metric_ids"] or "[]")
    check_cur = bool(row["check_currency_same"])
    check_grain = bool(row["check_granularity_same"])

    # Build name->expr map for all reachable metrics used in formula text
    name_to_expr: dict[str, str] = {}
    child_audits: list[dict[str, Any]] = []
    grains: list[set[str]] = []
    currencies: list[str] = []

    # Resolve declared sub metrics first
    id_to_name: dict[str, str] = {}
    for sid in sub_ids:
        expr, audit, grain = _resolve_metric(sid, currency, expr_template)
        name_to_expr[audit["name"]] = f"({expr})"
        id_to_name[sid] = audit["name"]
        child_audits.append(audit)
        grains.append(grain)
        currencies.append(currency)

    # Also allow nested composite names already in formula (e.g. 成本GAP)
    # Replace longest names first to avoid partial overlaps
    names_sorted = sorted(name_to_expr.keys(), key=len, reverse=True)
    expanded = formula
    for name in names_sorted:
        if name in expanded:
            expanded = expanded.replace(name, name_to_expr[name])

    # If formula still contains unresolved Chinese metric tokens, try lookup
    leftover = _find_unresolved_tokens(expanded)
    for token in leftover:
        expr, audit, grain = _resolve_metric(token, currency, expr_template)
        expanded = expanded.replace(token, f"({expr})")
        child_audits.append(audit)
        grains.append(grain)
        currencies.append(currency)

    if check_grain and grains:
        base = grains[0]
        for g in grains[1:]:
            if g != base:
                raise ValueError(f"复合指标粒度不一致 ({row['name']}): {base} vs {g}")

    if check_cur and currencies and len(set(currencies)) > 1:
        raise ValueError(f"复合指标币种不一致: {row['name']}")

    # Safety after expand: block statement separators / comments; allow filter predicates
    if re.search(r";|--|/\*|\*/", expanded):
        raise ValueError(f"公式展开后含非法片段: {expanded}")
    if not re.fullmatch(r"[\w.\s+\-*/(),'=<>!\u4e00-\u9fff]+", expanded, flags=re.UNICODE):
        raise ValueError(f"公式展开后含非法字符: {expanded}")

    bound_grain = db.get_metric_grain_codes("composite", row["id"])
    grain = set(bound_grain) if bound_grain else (grains[0] if grains else {"project_number"})
    analysis = db.get_metric_analysis_codes("composite", row["id"])
    audit = {
        "type": "composite",
        "id": row["id"],
        "name": row["name"],
        "formula": formula,
        "expanded_expr": expanded,
        "currency": currency,
        "children": child_audits,
        "grain": sorted(grain),
        "analysis_dims": analysis,
        "note": "先各阶段独立换算，再四则运算",
    }
    return expanded, audit, grain


def _same_stage_prefix(amount_col: str, rate_col: str) -> bool:
    """pj_total_cost <-> pj_exchange_rate etc."""
    a = amount_col.lower().replace("_total_cost", "").replace("_cost", "")
    r = rate_col.lower().replace("_exchange_rate", "").replace("_rate", "")
    # normalize finalcst
    return a.split("_")[0] == r.split("_")[0]


def _safe_alias(name: str, fallback: str) -> str:
    # SQLite identifiers: use ascii alias
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", fallback)
    if not alias or alias[0].isdigit():
        alias = "m_" + alias
    return alias


class _PreviewRow:
    """Minimal mapping for unsaved composite preview (sqlite3.Row-compatible)."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


def build_composite_sql_preview(
    *,
    formula: str,
    sub_metric_ids: list[str],
    currency: str = "CNY",
    check_currency_same: bool = True,
    check_granularity_same: bool = True,
    metric_id: str = "preview",
    alias: str | None = None,
) -> str:
    """Final SELECT for composite metric config UI (formula expanded + currency)."""
    formula = (formula or "").strip()
    if not formula:
        raise ValueError("公式不能为空")
    currency = (currency or "CNY").upper()
    rule = db.get_currency_rule(currency)
    if not rule:
        raise ValueError(f"不支持的币种: {currency}")

    row = _PreviewRow(
        {
            "id": metric_id or "preview",
            "name": alias or metric_id or "preview",
            "formula": formula,
            "sub_metric_ids": json.dumps(list(sub_metric_ids or [])),
            "check_currency_same": 1 if check_currency_same else 0,
            "check_granularity_same": 1 if check_granularity_same else 0,
        }
    )
    expr, _audit, _grain = _resolve_composite(row, currency, rule["expr_template"])
    fact_table = _fact_table_name()
    grain_col = _grain_field()
    sql_alias = _safe_alias(alias or "", metric_id or "cmp_preview")
    return (
        f"SELECT\n"
        f"  {FACT_ALIAS}.{grain_col} AS {grain_col},\n"
        f"  ({expr}) AS {sql_alias}\n"
        f"FROM {fact_table} {FACT_ALIAS}\n"
        f"ORDER BY {FACT_ALIAS}.{grain_col}"
    )


def _find_unresolved_tokens(expr: str) -> list[str]:
    """Find remaining CJK metric-like tokens (no ascii identifiers)."""
    # strip known SQL pieces
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]+", expr)
    out = []
    for t in tokens:
        if re.fullmatch(r"[0-9.]+", t):
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
            continue  # already SQL id
        if any("\u4e00" <= c <= "\u9fff" for c in t):
            out.append(t)
    # unique preserve order
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _execute(sql: str, params: list[Any]) -> tuple[list[str], list[list[Any]]]:
    with db.get_conn() as conn:
        cur = conn.execute(sql, params)
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        return columns, rows

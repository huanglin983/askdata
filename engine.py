"""Rule engine: metadata-driven SQL generation + execute + audit."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import db


FACT_ALIAS = "m"
DIM_ALIAS = "d"
FACT_TABLE = "ads_fin_tracker_comparison_summary_df"
DIM_TABLE = "dim_project"


@dataclass
class Intent:
    metric_ids: list[str] = field(default_factory=list)  # derived or composite ids
    currency: str = "CNY"
    dims: list[str] = field(default_factory=list)  # e.g. power_plant, region
    filters: dict[str, str] = field(default_factory=dict)  # project_number=
    raw_text: str = ""


@dataclass
class EngineResult:
    ok: bool
    sql: str = ""
    intent: dict[str, Any] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    error: str = ""


def run(intent: Intent) -> EngineResult:
    """Full pipeline: load meta -> bind -> currency -> check -> join -> SQL -> execute."""
    intent_dict = {
        "metric_ids": intent.metric_ids,
        "currency": intent.currency,
        "dims": intent.dims,
        "filters": intent.filters,
        "raw_text": intent.raw_text,
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
        # validate requested analysis dims against catalog + metric binds
        dim_defs: list[Any] = []
        for code in intent.dims:
            d = db.get_analysis_dim_by_code(code)
            if not d or not d["enabled"]:
                return EngineResult(
                    ok=False,
                    intent=intent_dict,
                    error=f"分析维度未注册或已禁用: {code}",
                )
            if d["dim_role"] == "grain":
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
                # ask only uses derived/composite; resolve id if name
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
            # intersection: only dims bound to ALL selected metrics
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

        select_parts: list[str] = [f"{FACT_ALIAS}.project_number AS project_number"]
        audit: list[dict[str, Any]] = []
        need_dim_join = False
        grains: list[set[str]] = []

        for mid in intent.metric_ids:
            expr, meta_audit, grain = _resolve_metric(mid, currency, rule["expr_template"])
            alias = _safe_alias(meta_audit["name"], mid)
            select_parts.append(f"({expr}) AS {alias}")
            audit.append(meta_audit)
            grains.append(grain)

        # granularity check across selected metrics
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

        for d in dim_defs:
            field = d["source_field"]
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", field):
                return EngineResult(
                    ok=False, intent=intent_dict, error=f"非法维度字段: {field}"
                )
            if d["source_table"] == DIM_TABLE:
                select_parts.append(f"{DIM_ALIAS}.{field} AS {d['code']}")
                need_dim_join = True
            else:
                select_parts.append(f"{FACT_ALIAS}.{field} AS {d['code']}")

        wheres: list[str] = []
        params: list[Any] = []
        for k, v in (intent.filters or {}).items():
            if not v:
                continue
            if k == "project_number":
                wheres.append(f"{FACT_ALIAS}.project_number = ?")
                params.append(v)
                continue
            ddef = db.get_analysis_dim_by_code(k)
            if not ddef:
                continue
            if ddef["source_table"] == DIM_TABLE:
                need_dim_join = True
                wheres.append(f"{DIM_ALIAS}.{ddef['source_field']} = ?")
                params.append(v)
            else:
                wheres.append(f"{FACT_ALIAS}.{ddef['source_field']} = ?")
                params.append(v)

        sql = (
            f"SELECT\n  "
            + ",\n  ".join(select_parts)
            + f"\nFROM {FACT_TABLE} {FACT_ALIAS}"
        )
        if need_dim_join:
            sql += (
                f"\nLEFT JOIN {DIM_TABLE} {DIM_ALIAS}"
                f"\n  ON {FACT_ALIAS}.project_number = {DIM_ALIAS}.project_number"
            )
        if wheres:
            sql += "\nWHERE " + " AND ".join(wheres)
        sql += f"\nORDER BY {FACT_ALIAS}.project_number"

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
    """Return (sql_expr, audit, grain_set). Supports derived + composite (nested)."""
    derived = db.get_derived(metric_id)
    if derived:
        return _resolve_derived(derived, currency, expr_template)

    composite = db.get_composite(metric_id)
    if composite:
        return _resolve_composite(composite, currency, expr_template)

    # allow resolve by name as fallback
    derived = db.get_derived_by_name(metric_id)
    if derived:
        return _resolve_derived(derived, currency, expr_template)
    composite = db.get_composite_by_name(metric_id)
    if composite:
        return _resolve_composite(composite, currency, expr_template)

    raise ValueError(f"指标不存在: {metric_id}")


def _resolve_derived(
    row: sqlite3.Row, currency: str, expr_template: str
) -> tuple[str, dict[str, Any], set[str]]:
    amount_col = row["amount_col"]
    rate_col = row["rate_col"]
    # hard rule: same-stage prefix binding
    if not _same_stage_prefix(amount_col, rate_col) and currency != "ORIGIN":
        raise ValueError(
            f"跨阶段汇率混用禁止: {amount_col} vs {rate_col} ({row['name']})"
        )

    amount_ref = f"{FACT_ALIAS}.{amount_col}"
    rate_ref = f"{FACT_ALIAS}.{rate_col}"
    if currency == "ORIGIN":
        expr = expr_template.format(amount=amount_ref, rate=rate_ref)
    else:
        expr = expr_template.format(amount=amount_ref, rate=rate_ref)

    grain = set(db.get_metric_grain_codes("derived", row["id"]))
    analysis = db.get_metric_analysis_codes("derived", row["id"])
    audit = {
        "type": "derived",
        "id": row["id"],
        "name": row["name"],
        "stage_type": row["stage_type"],
        "amount_col": amount_col,
        "rate_col": rate_col,
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

    # Safety: only allow digits, ops, parens, dots, spaces, aliases after expand
    if not re.fullmatch(r"[0-9a-zA-Z_.\s+\-*/(),]+", expanded):
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

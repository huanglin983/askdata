"""Atomic/derived filter text and measure SQL generation."""
from __future__ import annotations

import json
import re
from typing import Any

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_FACT_ALIAS = "m"
_BAD_SQL = re.compile(r";|--|/\*|\*/")


def normalize_filter_text(raw: str | list | None) -> str:
    """Return a SQL predicate fragment (no WHERE). Empty = no filter.

    Accepts free-form text, or legacy JSON list ``[{field,op,value}]``.
    """
    if raw is None:
        return ""
    if isinstance(raw, list):
        return _legacy_list_to_predicate(raw)
    s = str(raw).strip()
    if not s or s in ("[]", "{}", "null", "None"):
        return ""
    if s.startswith("["):
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return s  # treat as literal text that happens to start with [
        if isinstance(data, list):
            return _legacy_list_to_predicate(data)
    return s


def filter_from_form(text: str | None) -> str:
    pred = normalize_filter_text(text)
    assert_safe_filter(pred)
    return pred


def assert_safe_filter(pred: str) -> None:
    if not pred:
        return
    if _BAD_SQL.search(pred):
        raise ValueError("过滤条件不能包含 ; 或 SQL 注释")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _legacy_list_to_predicate(data: list) -> str:
    parts: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if not field or not _IDENT.match(field):
            continue
        op = str(item.get("op") or "=").strip().upper()
        if op == "<>":
            op = "!="
        value = str(item.get("value") if item.get("value") is not None else "").strip()
        col = f"{_FACT_ALIAS}.{field}"
        if op == "IS NULL":
            parts.append(f"{col} IS NULL")
        elif op == "IS NOT NULL":
            parts.append(f"{col} IS NOT NULL")
        elif op == "IN":
            items = [x.strip() for x in value.split(",") if x.strip()]
            if items:
                parts.append(f"{col} IN ({', '.join(_sql_literal(x) for x in items)})")
        elif value != "":
            parts.append(f"{col} {op} {_sql_literal(value)}")
    return " AND ".join(parts)


def wrap_case(expr: str, filter_text: str | None, alias: str = _FACT_ALIAS) -> str:
    """Apply filter as CASE WHEN ... THEN expr END."""
    pred = normalize_filter_text(filter_text)
    if not pred:
        return expr
    assert_safe_filter(pred)
    return f"CASE WHEN ({pred}) THEN {expr} END"


def build_atomic_amount_expr(
    source_field: str,
    filter_text: str | None = None,
    *,
    alias: str = _FACT_ALIAS,
    sql_expr: str | None = None,
    sql_manual: bool = False,
) -> str:
    """Amount measure fragment (before currency)."""
    if sql_manual and sql_expr and sql_expr.strip():
        return sql_expr.strip()
    if not _IDENT.match(source_field or ""):
        raise ValueError(f"非法来源字段: {source_field}")
    base = f"{alias}.{source_field}"
    return wrap_case(base, filter_text, alias=alias)


def build_atomic_sql_preview(
    *,
    source_table: str,
    source_field: str,
    name_en: str,
    filter_text: str | None = None,
    alias: str = _FACT_ALIAS,
) -> str:
    """Default metric SQL for config UI.

    Format::
        SELECT
          m.<field> AS <name_en>
        FROM <table> m

    With filters, the measure becomes CASE WHEN (...) THEN m.<field> END.
    """
    en = (name_en or "").strip()
    if not _IDENT.match(en):
        raise ValueError("英文名称须为字母/数字/下划线，且不能以数字开头")
    if not _IDENT.match(source_field or ""):
        raise ValueError(f"非法来源字段: {source_field}")
    table = source_table or "ads_fin_tracker_comparison_summary_df"
    if not _IDENT.match(table):
        raise ValueError(f"非法来源表: {table}")
    amount = build_atomic_amount_expr(source_field, filter_text, alias=alias)
    return (
        f"SELECT\n"
        f"  {amount} AS {en}\n"
        f"FROM {table} {alias}"
    )


def row_filter_text(row: Any, key: str = "filter_json") -> str:
    if row is None:
        return ""
    try:
        raw = row[key]
    except (KeyError, IndexError):
        return ""
    return normalize_filter_text(raw)


def assert_executable_select(sql: str) -> str:
    """Validate a single SELECT for preview execution; return normalized SQL."""
    text = (sql or "").strip().rstrip(";")
    if not text:
        raise ValueError("SQL 不能为空")
    if _BAD_SQL.search(text):
        raise ValueError("SQL 不能包含 ; 或注释")
    # strip leading whitespace/newlines for starts-with check
    head = text.lstrip().upper()
    if not head.startswith("SELECT"):
        raise ValueError("仅允许执行 SELECT 语句")
    if re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|REPLACE|PRAGMA)\b", text, re.I):
        raise ValueError("仅允许执行查询，禁止写操作")
    return text


def _same_stage_prefix(amount_col: str, rate_col: str) -> bool:
    """pj_total_cost <-> pj_exchange_rate etc."""
    a = amount_col.lower().replace("_total_cost", "").replace("_cost", "")
    r = rate_col.lower().replace("_exchange_rate", "").replace("_rate", "")
    return a.split("_")[0] == r.split("_")[0]


def build_derived_sql_preview(
    *,
    source_table: str,
    amount_col: str,
    rate_col: str,
    expr_template: str,
    atomic_filter: str | None = None,
    derived_filter: str | None = None,
    alias: str = "metric",
    grain_col: str = "project_number",
    currency: str = "CNY",
    sql_expr: str | None = None,
    sql_manual: bool = False,
) -> str:
    """Final SELECT for derived metric config UI (currency-converted measure).

    Mirrors engine ``_resolve_derived`` measure logic, wrapped as::

        SELECT
          m.<grain> AS <grain>,
          (<currency expr>) AS <alias>
        FROM <table> m
        ORDER BY m.<grain>
    """
    if not _IDENT.match(amount_col or ""):
        raise ValueError(f"非法金额字段: {amount_col}")
    if not _IDENT.match(rate_col or ""):
        raise ValueError(f"非法汇率字段: {rate_col}")
    table = source_table or "ads_fin_tracker_comparison_summary_df"
    if not _IDENT.match(table):
        raise ValueError(f"非法来源表: {table}")
    if not _IDENT.match(grain_col or ""):
        raise ValueError(f"非法粒度字段: {grain_col}")
    en = (alias or "metric").strip()
    if not _IDENT.match(en):
        en = re.sub(r"[^a-zA-Z0-9_]", "_", en) or "metric"
        if en[0].isdigit():
            en = "m_" + en

    cur = (currency or "CNY").upper()
    if cur != "ORIGIN" and not _same_stage_prefix(amount_col, rate_col):
        raise ValueError(f"跨阶段汇率混用禁止: {amount_col} vs {rate_col}")

    # Manual rewrite: full SELECT is documentation; non-SELECT fragment used as amount expr.
    if sql_manual and sql_expr and sql_expr.strip():
        manual = sql_expr.strip()
        if manual.upper().startswith("SELECT"):
            amount_expr = build_atomic_amount_expr(
                amount_col, atomic_filter, sql_manual=False
            )
        else:
            amount_expr = manual
    else:
        amount_expr = build_atomic_amount_expr(
            amount_col, atomic_filter, sql_manual=False
        )

    derived_pred = normalize_filter_text(derived_filter)
    if derived_pred:
        amount_expr = wrap_case(f"({amount_expr})", derived_pred)

    rate_ref = f"{_FACT_ALIAS}.{rate_col}"
    measure = expr_template.format(amount=amount_expr, rate=rate_ref)
    return (
        f"SELECT\n"
        f"  {_FACT_ALIAS}.{grain_col} AS {grain_col},\n"
        f"  ({measure}) AS {en}\n"
        f"FROM {table} {_FACT_ALIAS}\n"
        f"ORDER BY {_FACT_ALIAS}.{grain_col}"
    )

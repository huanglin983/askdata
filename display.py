"""将问数结果中的意图 / 审计转为中文展示结构。"""
from __future__ import annotations

import json
from typing import Any

import db
import meta

CURRENCY_CN = {
    "CNY": "人民币(CNY)",
    "USD": "美元(USD)",
    "ORIGIN": "原币(ORIGIN)",
}

SOURCE_CN = {
    "form": "结构化表单",
    "keyword": "关键词回退",
    "bailian": "LLM结构化抽取",
    "chatbi": "ChatBI意图流水线",
}

TYPE_CN = {
    "derived": "派生指标",
    "composite": "复合指标",
    "atomic": "原子指标",
    "dict": "指标字典",
}


def _metric_name(mid: str) -> str:
    row = db.get_derived(mid) or db.get_derived_by_name(mid)
    if row:
        return row["name"]
    row = db.get_composite(mid) or db.get_composite_by_name(mid)
    if row:
        return row["name"]
    row = db.get_atomic(mid) or db.get_atomic_by_name(mid)
    if row:
        return row["name"]
    return mid


def _dim_name(code: str) -> str:
    d = meta.get_analysis_field_by_code(code)
    return d["name"] if d else code


def intent_zh(intent: dict[str, Any] | None) -> dict[str, Any]:
    if not intent:
        return {}
    # Prefer canonical Chinese payload from LLM / form / keyword
    payload = intent.get("payload_zh")
    if isinstance(payload, dict) and payload:
        out = dict(payload)
        # ensure 意图来源 display
        src = intent.get("source") or ""
        if not out.get("意图来源"):
            out["意图来源"] = SOURCE_CN.get(src, src or "—")
        return out

    filters = intent.get("filters") or {}
    filters_zh = {}
    for k, v in filters.items():
        filters_zh[_dim_name(k) if k != "project_number" else "项目编号"] = v
    src = intent.get("source") or ""
    names = intent.get("metric_names") or [
        _metric_name(m) for m in intent.get("metric_ids") or []
    ]
    out = {
        "意图来源": SOURCE_CN.get(src, src or "—"),
        "意图类型": intent.get("intent_type") or "数据查询",
        "指标": names,
        "币种": CURRENCY_CN.get(intent.get("currency", ""), intent.get("currency")),
        "分析维度": [_dim_name(c) for c in intent.get("dims") or []],
        "筛选条件": filters_zh,
        "计算指令": intent.get("calc_instruction"),
        "是否查询关联指标": bool(intent.get("include_related")),
        "原始问句": intent.get("raw_text") or "",
    }
    if intent.get("include_related") and intent.get("related_metric_ids"):
        out["关联指标"] = [_metric_name(m) for m in intent["related_metric_ids"]]
    return out



def _audit_one_zh(item: dict[str, Any]) -> dict[str, Any]:
    t = item.get("type")
    out: dict[str, Any] = {
        "类型": TYPE_CN.get(t, t),
        "指标名称": item.get("name"),
        "指标ID": item.get("id"),
    }
    if t == "derived":
        out.update(
            {
                "业务阶段": item.get("stage_type"),
                "来源原子": item.get("atomic_name") or item.get("atomic_id"),
                "金额字段": item.get("amount_col"),
                "汇率字段": item.get("rate_col"),
                "原子过滤": item.get("atomic_filters") or "(无)",
                "派生附加过滤": item.get("derived_filters") or "(无)",
                "SQL手工改写": "是" if item.get("sql_manual") else "否",
                "目标币种": CURRENCY_CN.get(item.get("currency", ""), item.get("currency")),
                "换算表达式": item.get("expr"),
                "粒度维度": [_dim_name(c) for c in item.get("grain") or []],
                "可分析维度": [_dim_name(c) for c in item.get("analysis_dims") or []],
            }
        )
    elif t == "atomic":
        out.update(
            {
                "业务阶段": item.get("stage_type"),
                "金额字段": item.get("amount_col"),
                "汇率字段": item.get("rate_col"),
                "原子过滤": item.get("atomic_filters") or "(无)",
                "SQL手工改写": "是" if item.get("sql_manual") else "否",
                "目标币种": CURRENCY_CN.get(item.get("currency", ""), item.get("currency")),
                "换算表达式": item.get("expr"),
                "粒度维度": [_dim_name(c) for c in item.get("grain") or []],
                "可分析维度": [_dim_name(c) for c in item.get("analysis_dims") or []],
            }
        )
    elif t == "composite":
        children = [_audit_one_zh(c) for c in item.get("children") or []]
        out.update(
            {
                "计算公式": item.get("formula"),
                "展开表达式": item.get("expanded_expr"),
                "目标币种": CURRENCY_CN.get(item.get("currency", ""), item.get("currency")),
                "粒度维度": [_dim_name(c) for c in item.get("grain") or []],
                "可分析维度": [_dim_name(c) for c in item.get("analysis_dims") or []],
                "说明": item.get("note"),
                "子指标审计": children,
            }
        )
    else:
        # fallback keep remaining keys in Chinese-ish dump
        for k, v in item.items():
            if k in ("type", "name", "id"):
                continue
            out[k] = v
    return out


def audit_zh(audit: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [_audit_one_zh(a) for a in (audit or [])]


def column_zh(col: str) -> str:
    if col == "project_number":
        return "项目编号"
    d = meta.get_analysis_field_by_code(col)
    if d:
        return d["name"]
    # metric alias like cmp_cost_gap
    row = db.get_derived(col) or db.get_composite(col)
    if row:
        return row["name"]
    # try match by id prefix
    for r in db.list_composite():
        if r["id"] == col:
            return r["name"]
    for r in db.list_derived():
        if r["id"] == col:
            return r["name"]
    return col


def dumps_zh(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)

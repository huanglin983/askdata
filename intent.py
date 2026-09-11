"""Simple intent parsing: structured form first, keyword NLP fallback."""
from __future__ import annotations

import re

import db
from engine import Intent
import meta


def from_form(
    metric_ids: list[str],
    currency: str = "CNY",
    dims: list[str] | None = None,
    project_number: str = "",
    region: str = "",
    power_plant: str = "",
    raw_text: str = "",
) -> Intent:
    filters = {}
    if project_number:
        filters["project_number"] = project_number.strip()
    if region:
        filters["region"] = region.strip()
    if power_plant:
        filters["power_plant"] = power_plant.strip()
    return Intent(
        metric_ids=[m for m in metric_ids if m],
        currency=(currency or "CNY").upper(),
        dims=dims or [],
        filters=filters,
        raw_text=raw_text or "",
    )


def from_text(text: str) -> Intent:
    """Keyword-only 'NLP': match metric names, currency, dims, project id."""
    text = (text or "").strip()
    intent = Intent(raw_text=text)

    # currency
    if re.search(r"美元|USD|美金", text, re.I):
        intent.currency = "USD"
    elif re.search(r"原币|ORIGIN|外币原值", text, re.I):
        intent.currency = "ORIGIN"
    elif re.search(r"人民币|CNY|RMB|块钱", text, re.I):
        intent.currency = "CNY"
    else:
        intent.currency = "CNY"

    # dims from meta field names / codes
    for d in meta.list_ask_analysis_fields():
        if d["name"] in text or d["code"] in text:
            if d["code"] not in intent.dims:
                intent.dims.append(d["code"])
    # legacy keywords
    if "电站" in text and "power_plant" not in intent.dims:
        intent.dims.append("power_plant")
    if ("区域" in text or "大区" in text) and "region" not in intent.dims:
        intent.dims.append("region")
    if ("项目名称" in text or "项目名" in text) and "project_name" not in intent.dims:
        intent.dims.append("project_name")

    # filters: P001 style / 项目P001
    m = re.search(r"(?:项目\s*)?(P\d{3,})", text, re.I)
    if m:
        intent.filters["project_number"] = m.group(1).upper()

    # metrics: prefer longer names (复合 before 派生 fragments)
    composites = list(db.list_composite())
    derived = list(db.list_derived(exposed_only=True))
    candidates = [(r["name"], r["id"], "composite") for r in composites] + [
        (r["name"], r["id"], "derived") for r in derived
    ]
    candidates.sort(key=lambda x: len(x[0]), reverse=True)

    found_ids: list[str] = []
    remaining = text
    for name, mid, _kind in candidates:
        if name and name in remaining:
            found_ids.append(mid)
            remaining = remaining.replace(name, " ", 1)

    # aliases
    alias_map = {
        "成本差距": "cmp_cost_gap",
        "GAP率": "cmp_cost_gap_rate",
        "gap率": "cmp_cost_gap_rate",
        "成本gap": "cmp_cost_gap",
        "成本Gap": "cmp_cost_gap",
    }
    for alias, mid in alias_map.items():
        if alias in text and mid not in found_ids:
            found_ids.append(mid)

    # stage shortcuts: 「PJ成本」「Contract成本」
    stage_pat = re.compile(
        r"(PJ|Contract|Target|YTD|FinalCST)\s*(总成本|成本)", re.I
    )
    for m in stage_pat.finditer(text):
        stage = m.group(1)
        # normalize FinalCST casing
        for d in derived:
            if d["stage_type"].lower() == stage.lower() and d["id"] not in found_ids:
                # only total cost derived
                if "total_cost" in d["id"]:
                    found_ids.append(d["id"])
                    break

    intent.metric_ids = found_ids
    return intent

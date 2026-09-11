"""Intent parsing: structured form, Bailian LLM, or keyword fallback."""
from __future__ import annotations

import logging
import os
import re

import db
import meta
from engine import Intent

logger = logging.getLogger(__name__)


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
        source="form",
    )


def from_text_keywords(text: str) -> Intent:
    """Keyword-only 'NLP': match metric names, currency, dims, project id."""
    text = (text or "").strip()
    intent = Intent(raw_text=text, source="keyword")

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
        for d in derived:
            if d["stage_type"].lower() == stage.lower() and d["id"] not in found_ids:
                if "total_cost" in d["id"]:
                    found_ids.append(d["id"])
                    break

    intent.metric_ids = found_ids
    return intent


def _provider_mode() -> str:
    """auto | bailian | keyword — from INTENT_PROVIDER env."""
    try:
        import intent_llm

        intent_llm.load_dotenv_file()
    except Exception:  # noqa: BLE001
        pass
    return (os.environ.get("INTENT_PROVIDER") or "auto").strip().lower()


def from_text(text: str) -> Intent:
    """Natural language → Intent via Bailian when configured, else keywords."""
    text = (text or "").strip()
    mode = _provider_mode()

    use_bailian = False
    if mode == "keyword":
        use_bailian = False
    elif mode == "bailian":
        use_bailian = True
    else:  # auto
        try:
            import intent_llm

            use_bailian = intent_llm.bailian_configured()
        except Exception:  # noqa: BLE001
            use_bailian = False

    if use_bailian:
        try:
            import intent_llm

            return intent_llm.from_text_bailian(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("bailian intent failed, fallback to keyword: %s", e)
            intent = from_text_keywords(text)
            # keep source as keyword after fallback; raw_text already set
            return intent

    return from_text_keywords(text)

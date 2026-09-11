"""Intent parsing: structured form, Bailian LLM, or keyword fallback."""
from __future__ import annotations

import logging
import os
import re

import db
import meta
from engine import Intent

logger = logging.getLogger(__name__)

_RELATED_HINT = re.compile(
    r"相关指标|关联指标|对应的原子|派生指标|依赖指标|配套指标|相关的原子|以及其他相关"
)


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
    names = []
    for mid in metric_ids or []:
        row = db.get_composite(mid) or db.get_derived(mid) or db.get_atomic(mid)
        names.append(row["name"] if row else mid)
    cur = (currency or "CNY").upper()
    cur_label = {"CNY": "人民币(CNY)", "USD": "美元(USD)", "ORIGIN": "原币"}.get(
        cur, cur
    )
    intent = Intent(
        metric_ids=[m for m in metric_ids if m],
        currency=cur,
        dims=dims or [],
        filters=filters,
        raw_text=raw_text or "",
        source="form",
        intent_type="数据查询",
        include_related=False,
        calc_instruction=None,
        metric_names=names,
    )
    intent.payload_zh = {
        "意图来源": "结构化表单",
        "意图类型": "数据查询",
        "指标": names,
        "币种": cur_label,
        "分析维度": [
            (meta.get_analysis_field_by_code(c) or {}).get("name") or c
            for c in (dims or [])
        ],
        "筛选条件": {
            **({"项目编号": filters["project_number"]} if "project_number" in filters else {}),
            **({"区域": filters["region"]} if "region" in filters else {}),
            **({"电站": filters["power_plant"]} if "power_plant" in filters else {}),
        },
        "计算指令": None,
        "是否查询关联指标": False,
        "原始问句": raw_text or "",
    }
    return intent


def from_text_keywords(text: str) -> Intent:
    """Keyword-only fallback; fills Chinese payload_zh best-effort."""
    text = (text or "").strip()
    intent = Intent(raw_text=text, source="keyword", intent_type="数据查询")

    if re.search(r"口径|定义|计算公式|含义是什么|怎么算", text):
        intent.intent_type = "指标口径咨询"
    elif re.search(r"有哪些指标|指标清单|指标字典|指标列表", text):
        intent.intent_type = "指标字典检索"

    intent.include_related = bool(_RELATED_HINT.search(text))

    if re.search(r"美元|USD|美金", text, re.I):
        intent.currency = "USD"
    elif re.search(r"原币|ORIGIN|外币原值", text, re.I):
        intent.currency = "ORIGIN"
    elif re.search(r"人民币|CNY|RMB|块钱", text, re.I):
        intent.currency = "CNY"
    else:
        intent.currency = "CNY"

    for d in meta.list_ask_analysis_fields():
        if d["name"] in text or d["code"] in text:
            if d["code"] not in intent.dims:
                intent.dims.append(d["code"])
    if "电站" in text and "power_plant" not in intent.dims:
        if meta.get_analysis_field_by_code("power_plant"):
            intent.dims.append("power_plant")
    if ("区域" in text or "大区" in text) and "region" not in intent.dims:
        intent.dims.append("region")
    if ("项目名称" in text or "项目名" in text) and "project_name" not in intent.dims:
        intent.dims.append("project_name")

    m = re.search(r"(?:项目\s*)?(P\d{3,})", text, re.I)
    if m:
        intent.filters["project_number"] = m.group(1).upper()

    if re.search(r"\btop\b|排序|从大到小|合计|汇总|对比", text, re.I):
        intent.calc_instruction = "排序/汇总类（关键词识别）"

    composites = list(db.list_composite())
    derived = list(db.list_derived(exposed_only=True))
    candidates = [(r["name"], r["id"], "composite") for r in composites] + [
        (r["name"], r["id"], "derived") for r in derived
    ]
    candidates.sort(key=lambda x: len(x[0]), reverse=True)

    found_ids: list[str] = []
    found_names: list[str] = []
    remaining = text
    for name, mid, _kind in candidates:
        if name and name in remaining:
            found_ids.append(mid)
            found_names.append(name)
            remaining = remaining.replace(name, " ", 1)

    alias_map = {
        "成本差距": ("cmp_cost_gap", "成本GAP"),
        "GAP率": ("cmp_cost_gap_rate", "成本GAP率"),
        "gap率": ("cmp_cost_gap_rate", "成本GAP率"),
        "成本gap": ("cmp_cost_gap", "成本GAP"),
        "成本Gap": ("cmp_cost_gap", "成本GAP"),
    }
    for alias, (mid, standard) in alias_map.items():
        if alias in text and mid not in found_ids:
            found_ids.append(mid)
            found_names.append(standard)

    stage_pat = re.compile(
        r"(PJ|Contract|Target|YTD|FinalCST)\s*(总成本|成本)", re.I
    )
    for m in stage_pat.finditer(text):
        stage = m.group(1)
        for d in derived:
            if d["stage_type"].lower() == stage.lower() and d["id"] not in found_ids:
                if "total_cost" in d["id"]:
                    found_ids.append(d["id"])
                    found_names.append(d["name"])
                    break

    intent.metric_ids = found_ids
    intent.metric_names = found_names

    related_names: list[str] = []
    if intent.include_related and found_ids:
        try:
            import intent_llm

            related = intent_llm.lineage_related_ids(found_ids)
            intent.related_metric_ids = related
            cat = intent_llm.build_catalog()
            related_names = [
                cat["id_to_name"].get(i, i) for i in related if i in cat["id_to_name"]
            ]
            if intent.intent_type == "数据查询":
                for rid in intent_llm._queryable_metric_ids(related):
                    if rid not in intent.metric_ids:
                        intent.metric_ids.append(rid)
        except Exception as e:  # noqa: BLE001
            logger.warning("keyword related expand failed: %s", e)

    cur_label = {"CNY": "人民币(CNY)", "USD": "美元(USD)", "ORIGIN": "原币"}.get(
        intent.currency, intent.currency
    )
    filters_zh = {}
    if "project_number" in intent.filters:
        filters_zh["项目编号"] = intent.filters["project_number"]
    if "region" in intent.filters:
        filters_zh["区域"] = intent.filters["region"]
    if "power_plant" in intent.filters:
        filters_zh["电站"] = intent.filters["power_plant"]

    payload = {
        "意图来源": "关键词回退",
        "意图类型": intent.intent_type,
        "指标": found_names,
        "币种": cur_label,
        "分析维度": [
            (meta.get_analysis_field_by_code(c) or {}).get("name") or c
            for c in intent.dims
        ],
        "筛选条件": filters_zh,
        "计算指令": intent.calc_instruction,
        "是否查询关联指标": intent.include_related,
        "原始问句": text,
    }
    if intent.include_related:
        payload["关联指标"] = related_names
    intent.payload_zh = payload
    return intent


def _provider_mode() -> str:
    try:
        import intent_llm

        intent_llm.load_dotenv_file()
    except Exception:  # noqa: BLE001
        pass
    return (os.environ.get("INTENT_PROVIDER") or "auto").strip().lower()


def from_text(text: str, *, session_id: str | None = None) -> Intent:
    """自然语言 → Intent。

    主链路：ChatBI 意图流水线（百炼 LLM 或规则兜底）。
    失败回退：旧百炼单段抽取 → 关键词。
    INTENT_PROVIDER: auto | chatbi | bailian | keyword
    """
    text = (text or "").strip()
    mode = _provider_mode()

    if mode == "keyword":
        return from_text_keywords(text)

    # ---- 主链路 ChatBI ----
    if mode in ("auto", "chatbi", "bailian"):
        try:
            import intent_chatbi

            return intent_chatbi.from_text_chatbi(text, session_id=session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("chatbi intent failed, fallback: %s", e)
            if mode == "chatbi":
                return from_text_keywords(text)

    # ---- 回退：旧百炼 / 关键词 ----
    use_bailian = False
    if mode == "bailian":
        use_bailian = True
    elif mode == "auto":
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
            return from_text_keywords(text)

    return from_text_keywords(text)

"""Bailian intent: Chinese structured extraction schema + whitelist normalize.

LLM does entity extraction only — never SQL / numeric computation.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from askdata.engine import EngineResult, Intent
from askdata.infra import db
from askdata.meta import tables as meta

logger = logging.getLogger(__name__)

INTENT_TYPES = frozenset({"数据查询", "指标口径咨询", "指标字典检索"})
_ALLOWED_CURRENCIES = frozenset({"CNY", "USD", "ORIGIN"})

# 问数域信号：有此类信号时，即使未抽出指标也不应降为「未识别」
_BI_QUERY_HINT = re.compile(
    r"查|查询|看下|看看|统计|汇总|对比|报表|指标|金额|成本|GAP|gap|"
    r"人民币|美元|CNY|USD|项目|电站|区域|数据|P\d{3,}",
    re.I,
)
_ALL_METRICS_HINT = re.compile(
    r"所有指标|全部指标|所有的指标|全部的指标|每个指标|各项指标|全部数据|所有数据"
)

CAPABILITY_HELP = (
    "暂未识别到明确意图。我目前可以帮你：\n"
    "1. 数据查询：查指标数值（生成并执行 SQL）\n"
    "2. 指标口径咨询：问定义/公式/口径，返回指标元数据\n"
    "3. 指标字典检索：查有哪些指标/清单"
)

SUPPORTED_CAPABILITIES = [
    {
        "type": "数据查询",
        "desc": "查指标数值，走规则引擎生成/执行 SQL",
    },
    {
        "type": "指标口径咨询",
        "desc": "问定义/公式/口径，返回指标元数据（不查数）",
    },
    {
        "type": "指标字典检索",
        "desc": "查有哪些指标/清单，返回字典列表",
    },
]

# Display currency labels ↔ engine codes
_CURRENCY_LABEL_TO_CODE = {
    "人民币(CNY)": "CNY",
    "人民币": "CNY",
    "CNY": "CNY",
    "RMB": "CNY",
    "元": "CNY",
    "美元(USD)": "USD",
    "美元": "USD",
    "USD": "USD",
    "美金": "USD",
    "原币(ORIGIN)": "ORIGIN",
    "原币": "ORIGIN",
    "ORIGIN": "ORIGIN",
}
_CURRENCY_CODE_TO_LABEL = {
    "CNY": "人民币(CNY)",
    "USD": "美元(USD)",
    "ORIGIN": "原币",
}

# Analysis dim keyword → preferred code (then filtered by catalog)
_DIM_KEYWORDS: list[tuple[str, str]] = [
    ("项目编号", "project_number"),
    ("项目名称", "project_name"),
    ("项目名", "project_name"),
    ("电站", "power_plant"),
    ("区域", "region"),
    ("大区", "region"),
    ("阶段", "stage_type"),
    ("年份", "year"),
    ("月份", "month"),
    ("项目", "project_number"),  # last: broad
]

# Synonyms shown to LLM; output must use 标准名 only
_NAME_SYNONYMS: dict[str, list[str]] = {
    "成本GAP": ["成本差距", "成本gap", "成本Gap", "GAP", "gap"],
    "成本GAP率": ["GAP率", "gap率", "成本gap率"],
}

_FILTER_KEYS = frozenset(
    {
        "project_number",
        "region",
        "power_plant",
        "project_name",
        "stage_type",
        "year",
        "month",
        "时间范围",
        "项目范围",
        "阶段",
    }
)

_FILTER_KEY_ALIASES = {
    "项目编号": "project_number",
    "项目": "project_number",
    "项目号": "project_number",
    "区域": "region",
    "大区": "region",
    "电站": "power_plant",
    "项目名称": "project_name",
    "阶段": "stage_type",
    "阶段名称": "stage_type",
    "年份": "year",
    "年": "year",
    "月份": "month",
    "月": "month",
}


def load_dotenv_file(path: str | None = None) -> None:
    env_path = path or str(Path(__file__).resolve().parents[2] / ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError as e:
        logger.warning("failed to read .env: %s", e)


def bailian_configured() -> bool:
    load_dotenv_file()
    return bool((os.environ.get("DASHSCOPE_API_KEY") or "").strip())


def build_catalog() -> dict[str, Any]:
    """Full metric dictionary (atomic/derived/composite) for exact-name matching."""
    metrics: list[dict[str, str]] = []
    name_to_id: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    id_set: set[str] = set()
    id_to_type: dict[str, str] = {}

    for r in db.list_atomic():
        mid, name = r["id"], (r["name"] or "").strip()
        metrics.append({"id": mid, "name": name, "type": "atomic"})
        id_set.add(mid)
        id_to_type[mid] = "atomic"
        id_to_name[mid] = name
        if name:
            name_to_id[name] = mid

    for r in db.list_derived():  # include non-exposed for lineage / dict
        mid, name = r["id"], (r["name"] or "").strip()
        metrics.append({"id": mid, "name": name, "type": "derived"})
        id_set.add(mid)
        id_to_type[mid] = "derived"
        id_to_name[mid] = name
        if name:
            name_to_id[name] = mid

    for r in db.list_composite():
        mid, name = r["id"], (r["name"] or "").strip()
        metrics.append({"id": mid, "name": name, "type": "composite"})
        id_set.add(mid)
        id_to_type[mid] = "composite"
        id_to_name[mid] = name
        if name:
            name_to_id[name] = mid

    # synonym → standard name only (not free-form fuzzy)
    synonym_to_standard: dict[str, str] = {}
    for standard, syns in _NAME_SYNONYMS.items():
        if standard not in name_to_id:
            continue
        for s in syns:
            synonym_to_standard[s] = standard
            synonym_to_standard[s.lower()] = standard

    dims = [
        {"code": d["code"], "name": d["name"]}
        for d in meta.list_ask_analysis_fields()
    ]
    # also expose grain / common codes for keyword mapping even if not analysis
    dim_codes = {d["code"] for d in dims}
    for code in ("project_number", "power_plant", "region", "project_name"):
        if code not in dim_codes:
            # still allow as filter key; analysis dim only if meta says so
            pass

    currencies = [
        {"code": c["code"], "name": c["name"]} for c in db.list_currency_rules()
    ]
    return {
        "metrics": metrics,
        "name_to_id": name_to_id,
        "id_to_name": id_to_name,
        "id_to_type": id_to_type,
        "id_set": id_set,
        "synonym_to_standard": synonym_to_standard,
        "dims": dims,
        "dim_codes": dim_codes,
        "currencies": currencies,
    }


def build_messages(text: str, catalog: dict[str, Any]) -> list[dict[str, str]]:
    metric_lines = []
    queryable_names: list[str] = []
    for m in catalog["metrics"]:
        syns = _NAME_SYNONYMS.get(m["name"]) or []
        syn_part = f"（同义词仅作理解：{'、'.join(syns)}）" if syns else ""
        metric_lines.append(f"- {m['name']} [{m['type']}]{syn_part}")
        if m.get("type") in ("derived", "composite") and m.get("name"):
            queryable_names.append(m["name"])
    metric_block = "\n".join(metric_lines) or "(无)"
    all_metrics_json = json.dumps(queryable_names, ensure_ascii=False)

    dim_block = (
        "\n".join(f"- {d['name']} (code={d['code']})" for d in catalog["dims"])
        or "（元数据暂无勾选分析维度；仍可识别口语维度词写入分析维度数组，由服务端过滤）"
    )

    system = f"""你是指标平台的自然语言意图抽取器，仅做实体抽取，严禁生成SQL、不计算数值。
输入：用户的原始查询问句；输出严格输出JSON，不要额外解释、不要markdown。
固定输出字段定义（必须全部返回，不存在的值填空数组/空字符串/null）
{{
    "意图来源": "LLM结构化抽取",
    "意图类型": "数据查询|指标口径咨询|指标字典检索|未识别",
    "指标": [],
    "币种": "",
    "分析维度": [],
    "筛选条件": {{}},
    "计算指令": null,
    "是否查询关联指标": false,
    "原始问句": ""
}}
枚举规则
1. 意图类型判定规则（务必区分）
- 数据查询：用户想获取指标数值、看报表/项目数据（含「查询…数据」「看…指标」等），即使未点名具体指标名也判为数据查询
- 指标口径咨询：用户问指标定义、计算公式、口径、含义（不查数值）
- 指标字典检索：用户想查找有哪些指标、指标清单（只要清单不要数值）
- 未识别：与指标/报表/口径完全无关的闲聊或其它请求（如唱歌、天气、写诗）→ 必须标未识别，不要强行标数据查询
2. 「所有指标 / 全部指标 / 所有指标数据」
- 意图类型=数据查询
- 指标数组填写下列「平台指标字典」中全部【derived/composite】标准名称（不要只留空）
- 同时识别筛选条件（如项目编号 P001）
3. 是否查询关联指标判定
当问句包含：相关指标、关联指标、对应的原子、派生指标、依赖指标、配套指标 → 设置为true；其余false
例：“成本GAP以及其他相关的原子和派生指标” → 是否查询关联指标=true
4. 币种识别关键词
人民币、CNY、元 → "人民币(CNY)"；美元、USD、美金 → "美元(USD)"；原币、本位币以外原始货币 → "原币"
5. 分析维度识别关键词：项目、项目编号、电站、区域、阶段、年份、月份
6. 筛选条件：识别时间范围、项目编号（如P001）、区域、电站等（键用中文或英文均可，如 项目编号/project_number）
7. 计算指令识别关键词：top、排序、从大到小、合计、汇总、对比；无则 null

约束规则
1. 输出只能是纯JSON，不能附带任何说明文字、注释、代码块标记。
2. 识别不到内容，对应字段填空、[]或者null，不要编造不存在的指标名。
3. 指标名称必须严格使用下列「平台指标字典」中的标准名称；同义词只用于理解，输出仍写标准名。
4. 【是否查询关联指标=true】含义：只取当前指标血缘上下游依赖指标，不是全平台所有指标（服务端会展开血缘）。
5. 原始问句必须原样回填用户输入。
6. 闲聊/非问数 → 意图类型必须为「未识别」，指标=[]。

平台指标字典：
{metric_block}

当前可分析维度（供参考）：
{dim_block}

示例1输入：所有项目的成本GAP以及其他相关的原子和派生指标，人民币
示例1输出：
{{"意图来源":"LLM结构化抽取","意图类型":"数据查询","指标":["成本GAP"],"币种":"人民币(CNY)","分析维度":[],"筛选条件":{{}},"计算指令":null,"是否查询关联指标":true,"原始问句":"所有项目的成本GAP以及其他相关的原子和派生指标，人民币"}}

示例2输入：查询项目号P001的所有指标数据
示例2输出：
{{"意图来源":"LLM结构化抽取","意图类型":"数据查询","指标":{all_metrics_json},"币种":"","分析维度":[],"筛选条件":{{"项目编号":"P001"}},"计算指令":null,"是否查询关联指标":false,"原始问句":"查询项目号P001的所有指标数据"}}

示例3输入：可以给我唱首歌？
示例3输出：
{{"意图来源":"LLM结构化抽取","意图类型":"未识别","指标":[],"币种":"","分析维度":[],"筛选条件":{{}},"计算指令":null,"是否查询关联指标":false,"原始问句":"可以给我唱首歌？"}}
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def call_bailian_raw(messages: list[dict[str, str]]) -> str:
    """调用百炼，返回原始文本（供 ChatBI LLMSemanticParser 自行 JSON 清洗）。"""
    load_dotenv_file()
    api_key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")

    base_url = (
        os.environ.get("DASHSCOPE_BASE_URL")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).strip().rstrip("/")
    model = (os.environ.get("DASHSCOPE_MODEL") or "qwen-plus").strip()
    timeout = float(os.environ.get("DASHSCOPE_TIMEOUT") or "60")

    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("请先安装 openai：pip install openai") from e

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
    }
    try:
        resp = client.chat.completions.create(
            **kwargs, response_format={"type": "json_object"}
        )
    except Exception:
        resp = client.chat.completions.create(**kwargs)

    return (resp.choices[0].message.content or "").strip()


def call_bailian(messages: list[dict[str, str]]) -> dict[str, Any]:
    content = call_bailian_raw(messages)
    return _parse_json_content(content)


def _parse_json_content(content: str) -> dict[str, Any]:
    if not content:
        raise ValueError("模型返回空内容")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            raise ValueError(f"模型未返回 JSON: {content[:200]}")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("模型 JSON 根节点必须是对象")
    return data


def _get(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in raw:
            return raw[k]
    return default


def _match_metric_name(token: str, catalog: dict[str, Any]) -> str | None:
    """Return standard dictionary name or None. No fuzzy guess beyond synonym map."""
    token = (token or "").strip()
    if not token:
        return None
    name_to_id = catalog["name_to_id"]
    if token in name_to_id:
        return token
    # case-insensitive exact
    for name in name_to_id:
        if name.lower() == token.lower():
            return name
    syn = catalog["synonym_to_standard"]
    if token in syn:
        return syn[token]
    if token.lower() in syn:
        return syn[token.lower()]
    return None


def _parse_currency(label: Any) -> tuple[str, str]:
    """Return (engine_code, display_label)."""
    s = str(label or "").strip()
    if not s:
        return "CNY", ""
    code = _CURRENCY_LABEL_TO_CODE.get(s) or _CURRENCY_LABEL_TO_CODE.get(s.upper())
    if not code:
        # partial
        if re.search(r"美元|USD|美金", s, re.I):
            code = "USD"
        elif re.search(r"原币|ORIGIN", s, re.I):
            code = "ORIGIN"
        elif re.search(r"人民币|CNY|RMB|元", s, re.I):
            code = "CNY"
        else:
            code = "CNY"
    label_out = _CURRENCY_CODE_TO_LABEL.get(code, s)
    if code not in _ALLOWED_CURRENCIES:
        code, label_out = "CNY", "人民币(CNY)"
    return code, label_out


def _normalize_dims(raw_dims: Any, text: str, catalog: dict[str, Any]) -> list[str]:
    """Map LLM dim labels / codes to analysis dim codes present in meta."""
    dim_codes = catalog["dim_codes"]
    name_to_code = {d["name"]: d["code"] for d in catalog["dims"]}
    out: list[str] = []

    def add(code: str) -> None:
        if code in dim_codes and code not in out:
            # grain usually not as extra analysis dim
            d = meta.get_analysis_field_by_code(code)
            if d and (
                d.get("dim_role") == "grain" or d.get("semantic_role") == "grain"
            ):
                return
            out.append(code)

    for item in raw_dims or []:
        token = str(item).strip()
        if not token:
            continue
        if token in dim_codes:
            add(token)
            continue
        if token in name_to_code:
            add(name_to_code[token])
            continue
        for kw, code in _DIM_KEYWORDS:
            if kw == token or kw in token:
                add(code)
                break

    # light keyword enrich from original text for dims LLM missed
    for kw, code in _DIM_KEYWORDS:
        if kw in text:
            add(code)

    return out


def _normalize_filters(raw_filters: Any, text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(raw_filters, dict):
        for k, v in raw_filters.items():
            key = str(k).strip()
            key = _FILTER_KEY_ALIASES.get(key, key)
            if key not in _FILTER_KEYS and key not in (
                "project_number",
                "region",
                "power_plant",
                "project_name",
                "stage_type",
                "year",
                "month",
            ):
                # keep Chinese keys that look like filters for display; map known
                continue
            val = str(v).strip() if v is not None else ""
            if not val:
                continue
            if key == "project_number":
                val = val.upper()
            out[key] = val

    # keyword enrich project id
    if "project_number" not in out:
        m = re.search(r"(?:项目\s*)?(P\d{3,})", text, re.I)
        if m:
            out["project_number"] = m.group(1).upper()
    return out


def empty_payload_zh(raw_text: str = "") -> dict[str, Any]:
    return {
        "意图来源": "LLM结构化抽取",
        "意图类型": "数据查询",
        "指标": [],
        "币种": "",
        "分析维度": [],
        "筛选条件": {},
        "计算指令": None,
        "是否查询关联指标": False,
        "原始问句": raw_text or "",
    }


def lineage_related_ids(root_ids: list[str]) -> list[str]:
    """Bloodline: descendants (and direct atomic parents of derived). Not whole platform."""
    from askdata.maps import metric_map

    related: list[str] = []
    seen: set[str] = set(root_ids)

    def walk(mid: str) -> None:
        node = metric_map._expand(mid, visiting=set())
        if not node:
            return

        def collect(n: dict[str, Any]) -> None:
            cid = n["id"]
            if cid not in seen:
                seen.add(cid)
                related.append(cid)
            for ch in n.get("children") or []:
                collect(ch)

        # children only (exclude root itself from related list)
        for ch in node.get("children") or []:
            collect(ch)

    for rid in root_ids:
        walk(rid)

    # upstream: composites that list this id as sub-metric
    for c in db.list_composite():
        subs = json.loads(c["sub_metric_ids"] or "[]")
        if any(r in subs for r in root_ids) and c["id"] not in seen:
            seen.add(c["id"])
            related.append(c["id"])

    return related


def _queryable_metric_ids(ids: list[str]) -> list[str]:
    """Keep metrics the ask engine accepts as top-level (derived / composite).

    Atomics appear in 关联指标 display / 口径咨询, but data-query SELECT
    uses derived/composite only (engine dim-bind path rejects bare atomics).
    """
    out: list[str] = []
    for mid in ids:
        if db.get_composite(mid) or db.get_derived(mid):
            if mid not in out:
                out.append(mid)
    return out


def normalize_llm_payload(
    raw: dict[str, Any], catalog: dict[str, Any], *, raw_text: str
) -> Intent:
    """Map Chinese schema → Intent; build payload_zh for UI."""
    text = raw_text or str(_get(raw, "原始问句", "raw_text", default="") or "")

    intent_type = str(_get(raw, "意图类型", "intent_type", default="数据查询") or "").strip()
    if intent_type == "未识别":
        pass
    elif intent_type not in INTENT_TYPES:
        # soft map
        if any(k in intent_type for k in ("未识别", "unknown", "不清楚", "无法识别")):
            intent_type = "未识别"
        elif any(k in intent_type for k in ("口径", "定义", "公式", "含义")):
            intent_type = "指标口径咨询"
        elif any(k in intent_type for k in ("字典", "清单", "有哪些", "列表")):
            intent_type = "指标字典检索"
        else:
            intent_type = "数据查询"

    include_related = bool(
        _get(raw, "是否查询关联指标", "include_related", default=False)
    )
    calc = _get(raw, "计算指令", "calc_instruction", default=None)
    if calc is not None:
        calc = str(calc).strip() or None

    metric_names: list[str] = []
    metric_ids: list[str] = []
    for item in _get(raw, "指标", "metrics", "metric_ids", default=[]) or []:
        standard = _match_metric_name(str(item), catalog)
        if not standard:
            logger.info("drop non-dictionary metric: %s", item)
            continue
        mid = catalog["name_to_id"][standard]
        if mid not in metric_ids:
            metric_ids.append(mid)
            metric_names.append(standard)

    currency_code, currency_label = _parse_currency(
        _get(raw, "币种", "currency", default="")
    )
    dims = _normalize_dims(
        _get(raw, "分析维度", "dims", default=[]), text, catalog
    )
    filters = _normalize_filters(_get(raw, "筛选条件", "filters", default={}), text)

    related_ids: list[str] = []
    related_names: list[str] = []
    if include_related and metric_ids:
        related_ids = lineage_related_ids(metric_ids)
        related_names = [
            catalog["id_to_name"].get(i, i) for i in related_ids if i in catalog["id_to_name"]
        ]

    # For data query: merge queryable related into metric_ids
    query_ids = list(metric_ids)
    if include_related and intent_type == "数据查询":
        for rid in _queryable_metric_ids(related_ids):
            if rid not in query_ids:
                query_ids.append(rid)

    # Dim display labels
    dim_labels = []
    for code in dims:
        d = meta.get_analysis_field_by_code(code)
        dim_labels.append(d["name"] if d else code)

    # Filters display: prefer Chinese keys
    filters_zh: dict[str, str] = {}
    key_zh = {
        "project_number": "项目编号",
        "region": "区域",
        "power_plant": "电站",
        "project_name": "项目名称",
        "stage_type": "阶段",
        "year": "年份",
        "month": "月份",
    }
    for k, v in filters.items():
        filters_zh[key_zh.get(k, k)] = v

    payload_zh = {
        "意图来源": "LLM结构化抽取",
        "意图类型": intent_type,
        "指标": metric_names,
        "币种": currency_label,
        "分析维度": dim_labels,
        "筛选条件": filters_zh,
        "计算指令": calc,
        "是否查询关联指标": include_related,
        "原始问句": text,
    }
    if include_related:
        payload_zh["关联指标"] = related_names

    # 「所有指标」且指标仍空：服务端展开全部可查询指标（derived/composite）
    if (
        intent_type == "数据查询"
        and not query_ids
        and _ALL_METRICS_HINT.search(text)
    ):
        expand_ids = [
            m["id"]
            for m in catalog["metrics"]
            if m.get("type") in ("derived", "composite") and m.get("id")
        ]
        query_ids = _queryable_metric_ids(expand_ids)
        metric_names = [
            catalog["id_to_name"].get(i, i) for i in query_ids if i in catalog["id_to_name"]
        ]
        payload_zh["指标"] = metric_names
        payload_zh["展开说明"] = "问句含「所有指标」，已展开平台可查询指标"

    # 仅当无问数域信号时，空指标的「数据查询」才降为未识别（避免误伤真查询）
    if intent_type == "数据查询" and not query_ids and not metric_names:
        if _BI_QUERY_HINT.search(text) or filters:
            payload_zh["校验告警"] = "未点名具体指标，请补充指标名称；或改口「所有指标」以展开全部可查询指标"
        else:
            intent_type = "未识别"
            payload_zh["意图类型"] = "未识别"

    return Intent(
        metric_ids=query_ids,
        currency=currency_code,
        dims=dims,
        filters={
            k: v
            for k, v in filters.items()
            if k in ("project_number", "region", "power_plant", "project_name")
        },
        raw_text=text,
        source="bailian",
        intent_type=intent_type,
        include_related=include_related,
        calc_instruction=calc,
        metric_names=metric_names,
        related_metric_ids=related_ids,
        payload_zh=payload_zh,
    )


def from_text_bailian(text: str) -> Intent:
    text = (text or "").strip()
    catalog = build_catalog()
    messages = build_messages(text, catalog)
    raw = call_bailian(messages)
    raw.pop("sql", None)
    # force 原始问句
    raw["原始问句"] = text
    intent = normalize_llm_payload(raw, catalog, raw_text=text)
    return intent


def intent_to_engine_dict(intent: Intent) -> dict[str, Any]:
    base = {
        "metric_ids": intent.metric_ids,
        "currency": intent.currency,
        "dims": intent.dims,
        "filters": intent.filters,
        "raw_text": intent.raw_text,
        "source": intent.source or "",
        "intent_type": intent.intent_type,
        "include_related": intent.include_related,
        "calc_instruction": intent.calc_instruction,
        "metric_names": list(intent.metric_names),
        "related_metric_ids": list(intent.related_metric_ids),
        "payload_zh": dict(intent.payload_zh or {}),
    }
    return base


def needs_capability_help(intent: Intent) -> bool:
    """自然语言未落入可执行意图时，应返回能力引导而非强行跑 SQL。"""
    if getattr(intent, "capability_help_msg", None):
        return True
    if intent.intent_type == "未识别" or intent.intent_type not in INTENT_TYPES:
        return True
    if intent.intent_type == "数据查询" and not intent.metric_ids and not intent.metric_names:
        return True
    return False


def capability_help_result(intent: Intent) -> EngineResult:
    """未识别意图时的统一反馈。"""
    msg = getattr(intent, "capability_help_msg", None) or CAPABILITY_HELP
    # 展示层明确标为未识别，避免仍显示「数据查询」
    intent.intent_type = "未识别"
    if intent.payload_zh is not None:
        intent.payload_zh = dict(intent.payload_zh)
        intent.payload_zh["意图类型"] = "未识别"
        intent.payload_zh.setdefault(
            "能力清单", [c["type"] for c in SUPPORTED_CAPABILITIES]
        )
    intent_dict = intent_to_engine_dict(intent)
    payload = dict(intent_dict.get("payload_zh") or {})
    payload["意图类型"] = "未识别"
    payload.setdefault("能力清单", [c["type"] for c in SUPPORTED_CAPABILITIES])
    intent_dict["payload_zh"] = payload
    intent_dict["intent_type"] = "未识别"
    return EngineResult(ok=False, intent=intent_dict, error=msg)


def handle_non_query(intent: Intent) -> EngineResult | None:
    """Return EngineResult for 口径咨询 / 字典检索; None if should run SQL."""
    if intent.intent_type == "数据查询":
        return None

    intent_dict = intent_to_engine_dict(intent)
    catalog = build_catalog()

    if intent.intent_type == "指标字典检索":
        rows = []
        # if user named metrics, filter; else list all (or related)
        ids = list(intent.metric_ids) or [
            m["id"] for m in catalog["metrics"]
        ]
        if intent.include_related and intent.metric_ids:
            ids = list(dict.fromkeys(intent.metric_ids + intent.related_metric_ids))
        for mid in ids:
            mtype = catalog["id_to_type"].get(mid, "")
            name = catalog["id_to_name"].get(mid, mid)
            rows.append([name, mtype, mid])
        return EngineResult(
            ok=True,
            sql="",
            intent=intent_dict,
            audit=[{"type": "dict", "name": "指标字典", "id": "dict", "note": "字典检索"}],
            columns=["指标名称", "类型", "指标ID"],
            rows=rows,
        )

    if intent.intent_type == "指标口径咨询":
        if not intent.metric_ids and not intent.metric_names:
            return EngineResult(
                ok=False,
                intent=intent_dict,
                error="未识别到可咨询的指标，请使用平台字典中的标准名称",
            )
        audit = []
        ids = list(intent.metric_ids)
        if intent.include_related:
            ids = list(dict.fromkeys(ids + intent.related_metric_ids))
        for mid in ids:
            c = db.get_composite(mid)
            if c:
                audit.append(
                    {
                        "type": "composite",
                        "id": mid,
                        "name": c["name"],
                        "formula": c["formula"],
                        "note": "口径咨询（不执行数值查询）",
                        "grain": [],
                    }
                )
                continue
            d = db.get_derived(mid)
            if d:
                a = db.get_atomic(d["atomic_id"])
                audit.append(
                    {
                        "type": "derived",
                        "id": mid,
                        "name": d["name"],
                        "stage_type": d["stage_type"] or (a["stage_type"] if a else ""),
                        "atomic_id": d["atomic_id"],
                        "atomic_name": a["name"] if a else "",
                        "amount_col": (a["source_field"] if a else d["amount_col"]),
                        "rate_col": (a["rate_col"] if a else d["rate_col"]),
                        "note": "口径咨询（不执行数值查询）",
                    }
                )
                continue
            a = db.get_atomic(mid)
            if a:
                audit.append(
                    {
                        "type": "atomic",
                        "id": mid,
                        "name": a["name"],
                        "stage_type": a["stage_type"],
                        "amount_col": a["source_field"],
                        "rate_col": a["rate_col"],
                        "remark": a["remark"] if "remark" in a.keys() else "",
                        "note": "口径咨询（不执行数值查询）",
                    }
                )
        return EngineResult(
            ok=True,
            sql="",
            intent=intent_dict,
            audit=audit,
            columns=[],
            rows=[],
        )

    # 未覆盖的意图类型：能力引导，不落 SQL
    return capability_help_result(intent)

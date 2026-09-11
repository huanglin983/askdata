"""Bailian (DashScope OpenAI-compatible) intent parsing.

Model outputs structured Intent JSON only. Never SQL / business口径.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import db
import meta
from engine import Intent

logger = logging.getLogger(__name__)

_ALLOWED_CURRENCIES = frozenset({"CNY", "USD", "ORIGIN"})
_FILTER_KEYS = frozenset({"project_number", "region", "power_plant"})

# Common aliases → metric id (also in keyword path)
_ALIAS_TO_ID = {
    "成本差距": "cmp_cost_gap",
    "成本GAP": "cmp_cost_gap",
    "成本Gap": "cmp_cost_gap",
    "成本gap": "cmp_cost_gap",
    "GAP率": "cmp_cost_gap_rate",
    "gap率": "cmp_cost_gap_rate",
    "成本GAP率": "cmp_cost_gap_rate",
}


def load_dotenv_file(path: str | None = None) -> None:
    """Load KEY=VALUE from .env into os.environ if not already set (no extra deps)."""
    env_path = path or os.path.join(os.path.dirname(__file__), ".env")
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
    """Whitelist of metrics / dims / currencies for the prompt + normalize."""
    metrics: list[dict[str, str]] = []
    name_to_id: dict[str, str] = {}
    id_set: set[str] = set()

    for r in db.list_composite():
        mid, name = r["id"], r["name"] or ""
        metrics.append({"id": mid, "name": name, "type": "composite"})
        id_set.add(mid)
        if name:
            name_to_id[name] = mid
    for r in db.list_derived(exposed_only=True):
        mid, name = r["id"], r["name"] or ""
        metrics.append({"id": mid, "name": name, "type": "derived"})
        id_set.add(mid)
        if name:
            name_to_id[name] = mid

    for alias, mid in _ALIAS_TO_ID.items():
        if mid in id_set:
            name_to_id.setdefault(alias, mid)

    dims = [
        {"code": d["code"], "name": d["name"]}
        for d in meta.list_ask_analysis_fields()
    ]
    dim_codes = {d["code"] for d in dims}
    currencies = [
        {"code": c["code"], "name": c["name"]} for c in db.list_currency_rules()
    ]
    return {
        "metrics": metrics,
        "name_to_id": name_to_id,
        "id_set": id_set,
        "dims": dims,
        "dim_codes": dim_codes,
        "currencies": currencies,
        "currency_codes": {c["code"] for c in currencies} | set(_ALLOWED_CURRENCIES),
        "filter_keys": sorted(_FILTER_KEYS),
    }


def build_messages(text: str, catalog: dict[str, Any]) -> list[dict[str, str]]:
    metric_lines = "\n".join(
        f"- id={m['id']} name={m['name']} type={m['type']}" for m in catalog["metrics"]
    )
    dim_lines = "\n".join(
        f"- code={d['code']} name={d['name']}" for d in catalog["dims"]
    ) or "(无)"
    cur_lines = "\n".join(
        f"- code={c['code']} name={c['name']}" for c in catalog["currencies"]
    )
    system = f"""你是财务问数系统的意图解析器。只把用户问句解析为结构化 JSON，禁止编写 SQL、表名、汇率公式或业务口径。

必须只输出一个 JSON 对象，字段：
- metric_ids: string[]，只能使用下列指标的 id（优先）或中文 name（服务端会映射）
- currency: 只能是 CNY / USD / ORIGIN 之一
- dims: string[]，只能使用下列分析维度的 code
- filters: object，键只能是 {catalog["filter_keys"]} 中的键；值为用户提到的筛选值
- confidence: number 0~1

可选指标目录：
{metric_lines}

可选分析维度：
{dim_lines}

可选币种：
{cur_lines}

规则：
1. 未提到的维度不要填；未提到的筛选不要编造。
2. 指标别名：「成本GAP」「成本差距」「GAP」→ cmp_cost_gap；「GAP率」→ cmp_cost_gap_rate。
3. 维度口语：「带电站」「按电站」「电站」→ power_plant；「区域」「大区」→ region；「项目名称」→ project_name。
4. 筛选：「项目P001」「P001」→ filters.project_number=P001。
5. 币种：「人民币」→ CNY；「美元」→ USD；「原币」→ ORIGIN。
6. 不要输出除上述字段外的键（尤其不要 sql）。

示例问句：查询项目P001的成本GAP，人民币，带电站
示例 JSON：{{"metric_ids":["cmp_cost_gap"],"currency":"CNY","dims":["power_plant"],"filters":{{"project_number":"P001"}},"confidence":0.95}}
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def call_bailian(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Call DashScope OpenAI-compatible chat completions; return parsed JSON dict."""
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
    # Prefer JSON mode when supported
    try:
        resp = client.chat.completions.create(
            **kwargs, response_format={"type": "json_object"}
        )
    except Exception:
        resp = client.chat.completions.create(**kwargs)

    content = (resp.choices[0].message.content or "").strip()
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


def normalize_intent(
    raw: dict[str, Any], catalog: dict[str, Any], *, raw_text: str
) -> Intent:
    """Map names→ids, drop illegal items. Empty metric_ids → raise for fallback."""
    name_to_id: dict[str, str] = catalog["name_to_id"]
    id_set: set[str] = catalog["id_set"]
    dim_codes: set[str] = catalog["dim_codes"]
    currency_codes: set[str] = catalog["currency_codes"]

    metric_ids: list[str] = []
    for item in raw.get("metric_ids") or []:
        token = str(item).strip()
        if not token:
            continue
        if token in id_set:
            mid = token
        elif token in name_to_id:
            mid = name_to_id[token]
        elif token in _ALIAS_TO_ID and _ALIAS_TO_ID[token] in id_set:
            mid = _ALIAS_TO_ID[token]
        else:
            # case-insensitive name match
            mid = ""
            for name, nid in name_to_id.items():
                if name.lower() == token.lower():
                    mid = nid
                    break
            if not mid:
                logger.info("drop unknown metric token: %s", token)
                continue
        if mid not in metric_ids:
            metric_ids.append(mid)

    if not metric_ids:
        raise ValueError("百炼意图未识别到白名单内的指标")

    currency = str(raw.get("currency") or "CNY").strip().upper()
    if currency not in currency_codes or currency not in _ALLOWED_CURRENCIES:
        currency = "CNY"

    dims: list[str] = []
    for d in raw.get("dims") or []:
        code = str(d).strip()
        if code in dim_codes and code not in dims:
            dims.append(code)
        else:
            # allow match by Chinese name
            for dd in catalog["dims"]:
                if dd["name"] == code and dd["code"] not in dims:
                    dims.append(dd["code"])
                    break

    filters: dict[str, str] = {}
    raw_filters = raw.get("filters") or {}
    if isinstance(raw_filters, dict):
        for k, v in raw_filters.items():
            key = str(k).strip()
            if key not in _FILTER_KEYS:
                continue
            val = str(v).strip() if v is not None else ""
            if val:
                if key == "project_number":
                    val = val.upper()
                filters[key] = val

    return Intent(
        metric_ids=metric_ids,
        currency=currency,
        dims=dims,
        filters=filters,
        raw_text=raw_text,
        source="bailian",
    )


def from_text_bailian(text: str) -> Intent:
    catalog = build_catalog()
    messages = build_messages(text, catalog)
    raw = call_bailian(messages)
    # strip forbidden keys early
    raw.pop("sql", None)
    intent = normalize_intent(raw, catalog, raw_text=text)
    return _enrich_from_keywords(intent, text)


def _enrich_from_keywords(intent: Intent, text: str) -> Intent:
    """Fill missing dims/filters that keywords can detect (LLM often omits)."""
    # late import to avoid circular import at module load
    import intent as intent_mod

    kw = intent_mod.from_text_keywords(text)
    for d in kw.dims:
        if d not in intent.dims and d in build_catalog()["dim_codes"]:
            intent.dims.append(d)
    for k, v in (kw.filters or {}).items():
        if k not in intent.filters and v:
            intent.filters[k] = v
    if not intent.currency and kw.currency:
        intent.currency = kw.currency
    intent.source = "bailian"
    return intent

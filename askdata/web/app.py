"""Flask app: metric config CRUD + Text-to-SQL demo."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from askdata.engine import EngineResult, build_composite_sql_preview, run
from askdata.infra import db
from askdata.intent import form as intent_mod
from askdata.maps import metric_map, table_model_map
from askdata.meta import biz_arch, metric_sql, tables as meta
from . import display

_REPO_ROOT = Path(__file__).resolve().parents[2]
app = Flask(
    __name__,
    template_folder=str(_REPO_ROOT / "templates"),
    static_folder=str(_REPO_ROOT / "static"),
)
app.secret_key = "metric-t2sql-demo-dev"


@app.template_filter("zh_json")
def zh_json_filter(value):
    return display.dumps_zh(value)


@app.before_request
def _ensure_db():
    db.init_db()


def _dim_form_context(metric_type: str | None = None, metric_id: str | None = None):
    dims = meta.list_bindable_meta_fields()
    selected = (
        db.list_metric_dim_ids(metric_type, metric_id)
        if metric_type and metric_id
        else []
    )
    # new metric default: all bindable fields
    if not selected and not metric_id:
        selected = [d["id"] for d in dims]
    return dims, selected


def _metric_kw_match(row, q: str, fields: tuple[str, ...]) -> bool:
    """Case-insensitive substring match on any of the given row fields."""
    needle = (q or "").strip().lower()
    if not needle:
        return True
    for f in fields:
        try:
            val = row[f]
        except (KeyError, IndexError, TypeError):
            continue
        if val is not None and needle in str(val).lower():
            return True
    return False


_ATOMIC_SEARCH_FIELDS = (
    "id",
    "name",
    "name_en",
    "aliases",
    "source_field",
    "source_table",
    "stage_type",
    "rate_col",
    "biz_line",
    "theme_domain",
    "biz_object",
    "biz_process",
    "agg_type",
)
_DERIVED_SEARCH_FIELDS = (
    "id",
    "name",
    "aliases",
    "atomic_id",
    "stage_type",
    "amount_col",
    "rate_col",
    "biz_line",
    "theme_domain",
    "biz_object",
    "biz_process",
)
_COMPOSITE_SEARCH_FIELDS = (
    "id",
    "name",
    "aliases",
    "formula",
    "biz_line",
    "theme_domain",
    "biz_object",
    "biz_process",
)


def _metric_list_row(row) -> dict:
    """Row dict for list templates: include parsed alias_list."""
    d = dict(row)
    d["alias_list"] = db.parse_aliases(d.get("aliases") or "")
    return d


@app.route("/")
def index():
    return render_template(
        "index.html",
        atomic_n=len(db.list_atomic()),
        derived_n=len(db.list_derived()),
        composite_n=len(db.list_composite()),
        meta_n=len(meta.list_meta_tables()),
    )


# ---------- Metrics hub ----------
@app.route("/metrics")
def metrics_hub():
    return render_template(
        "metrics.html",
        atomic_n=len(db.list_atomic()),
        derived_n=len(db.list_derived()),
        composite_n=len(db.list_composite()),
    )


# ---------- Warehouse metadata ----------
@app.route("/meta/tables")
def meta_table_list():
    rows = []
    for t in meta.list_meta_tables():
        d = dict(t)
        d["field_n"] = len(meta.list_meta_fields(t["id"]))
        d["grain_keys"] = meta.get_table_grain_keys(t["id"])
        rows.append(d)
    return render_template("meta_table_list.html", rows=rows)


# ---------- Business architecture ----------
@app.route("/meta/biz-arch")
def biz_arch_view():
    parent_id = (request.args.get("parent_id") or "").strip() or None
    keyword = request.args.get("q") or ""
    enabled = request.args.get("enabled")  # '', '1', '0'
    parent = biz_arch.get_node(parent_id) if parent_id else None
    rows = biz_arch.list_children(parent_id, keyword=keyword, enabled=enabled or None)
    tree = biz_arch.build_tree()
    child_type = biz_arch.default_child_type(parent_id)
    return render_template(
        "biz_arch.html",
        tree=tree,
        rows=rows,
        parent=parent,
        parent_id=parent_id,
        keyword=keyword,
        enabled=enabled or "",
        child_type=child_type,
        child_type_label=biz_arch.NODE_TYPE_LABEL.get(child_type, child_type),
        type_labels=biz_arch.NODE_TYPE_LABEL,
    )


@app.route("/meta/biz-arch/edit", methods=["GET", "POST"])
@app.route("/meta/biz-arch/edit/<node_id>", methods=["GET", "POST"])
def biz_arch_edit(node_id: str | None = None):
    row = biz_arch.get_node(node_id) if node_id else None
    parent_id = (
        (row["parent_id"] if row else None)
        or (request.args.get("parent_id") or "").strip()
        or None
    )
    parent = biz_arch.get_node(parent_id) if parent_id else None
    node_type = (
        (row["node_type"] if row else None)
        or request.args.get("node_type")
        or biz_arch.default_child_type(parent_id)
    )
    if request.method == "POST":
        try:
            nid = biz_arch.upsert_node(
                {
                    "id": request.form.get("id") or node_id or "",
                    "parent_id": request.form.get("parent_id") or None,
                    "node_type": request.form.get("node_type") or node_type,
                    "name_zh": request.form["name_zh"],
                    "name_en": request.form.get("name_en") or "",
                    "enabled": 1 if request.form.get("enabled") else 0,
                    "sort_no": request.form.get("sort_no") or 100,
                    "remark": request.form.get("remark") or "",
                }
            )
            flash("业务架构已保存", "ok")
            saved = biz_arch.get_node(nid)
            back_parent = saved["parent_id"] if saved else parent_id
            return redirect(
                url_for("biz_arch_view", parent_id=back_parent or None)
            )
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            parent_id = (request.form.get("parent_id") or "").strip() or None
            parent = biz_arch.get_node(parent_id) if parent_id else None
            node_type = request.form.get("node_type") or node_type
    return render_template(
        "biz_arch_edit.html",
        row=row,
        parent=parent,
        parent_id=parent_id,
        node_type=node_type,
        type_label=biz_arch.NODE_TYPE_LABEL.get(node_type, node_type),
        type_labels=biz_arch.NODE_TYPE_LABEL,
    )


@app.route("/meta/biz-arch/batch", methods=["POST"])
def biz_arch_batch():
    action = (request.form.get("action") or "").strip()
    ids = request.form.getlist("ids")
    parent_id = (request.form.get("parent_id") or "").strip() or None
    try:
        if action == "enable":
            n = biz_arch.set_enabled(ids, 1)
            flash(f"已启用 {n} 条", "ok")
        elif action == "disable":
            n = biz_arch.set_enabled(ids, 0)
            flash(f"已停用 {n} 条", "ok")
        elif action == "delete":
            n = biz_arch.delete_nodes(ids)
            flash(f"已删除 {n} 条", "ok")
        else:
            flash("未知操作", "err")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("biz_arch_view", parent_id=parent_id))


@app.route("/meta/biz-arch/toggle/<node_id>", methods=["POST"])
def biz_arch_toggle(node_id: str):
    parent_id = (request.form.get("parent_id") or "").strip() or None
    try:
        node = biz_arch.get_node(node_id)
        if not node:
            raise ValueError("节点不存在")
        biz_arch.set_enabled([node_id], 0 if node["enabled"] else 1)
        flash("状态已更新", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("biz_arch_view", parent_id=parent_id))


@app.route("/meta/biz-arch/delete/<node_id>", methods=["POST"])
def biz_arch_delete(node_id: str):
    parent_id = (request.form.get("parent_id") or "").strip() or None
    try:
        biz_arch.delete_nodes([node_id])
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("biz_arch_view", parent_id=parent_id))


@app.route("/meta/tables/edit", methods=["GET", "POST"])
@app.route("/meta/tables/edit/<table_id>", methods=["GET", "POST"])
def meta_table_edit(table_id: str | None = None):
    row = meta.get_meta_table(table_id) if table_id else None
    fields = meta.list_meta_fields(table_id) if table_id else []
    physicals = meta.list_physical_tables()
    grain_keys = meta.get_table_grain_keys(table_id) if table_id else []
    if request.method == "POST":
        tid = request.form.get("id") or f"tbl_{uuid.uuid4().hex[:8]}"
        try:
            # grain_section marks that the business-key checklist was rendered
            grain_arg = (
                request.form.getlist("grain_keys")
                if "grain_section" in request.form
                else None
            )
            meta.upsert_meta_table(
                {
                    "id": tid,
                    "name": request.form["name"].strip(),
                    "physical_name": request.form["physical_name"].strip(),
                    "table_kind": request.form.get("table_kind", "dim"),
                    "enabled": 1 if request.form.get("enabled") else 0,
                    "remark": request.form.get("remark") or "",
                },
                sync_fields=True,
                grain_keys=grain_arg,
            )
            flash("表元数据已保存，字段已从物理表同步", "ok")
            return redirect(url_for("meta_table_edit", table_id=tid))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
    return render_template(
        "meta_table_edit.html",
        row=row,
        fields=fields,
        physicals=physicals,
        grain_keys=grain_keys,
    )


@app.route("/meta/tables/sync-fields/<table_id>", methods=["POST"])
def meta_table_sync_fields(table_id: str):
    try:
        n = meta.sync_fields_from_physical(table_id)
        flash(f"已同步字段，新增 {n} 个", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("meta_table_edit", table_id=table_id))


@app.route("/meta/tables/delete/<table_id>", methods=["POST"])
def meta_table_delete(table_id: str):
    try:
        meta.delete_meta_table(table_id)
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("meta_table_list"))


@app.route("/meta/fields/update/<field_id>", methods=["POST"])
def meta_field_update(field_id: str):
    table_id = request.form.get("table_id") or ""
    try:
        meta.update_meta_field(
            field_id,
            {
                "display_name": request.form.get("display_name") or "",
                "semantic_role": request.form.get("semantic_role") or "other",
                "is_analysis_dim": 1 if request.form.get("is_analysis_dim") else 0,
                "enabled": 1 if request.form.get("enabled") else 0,
                "sort_no": request.form.get("sort_no") or 100,
            },
        )
        flash("字段已更新", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("meta_table_edit", table_id=table_id))


@app.route("/meta/rels")
def meta_rel_list():
    return render_template("meta_rel_list.html", rows=meta.list_meta_rels())


@app.route("/meta/map")
def table_model_map_view():
    data = table_model_map.build_table_model_map()
    return render_template(
        "table_model_map.html",
        groups=data["groups"],
        orphan_dims=data["orphan_dims"],
        orphan_facts=data["orphan_facts"],
        stats=data["stats"],
    )


@app.route("/meta/rels/edit", methods=["GET", "POST"])
@app.route("/meta/rels/edit/<rel_id>", methods=["GET", "POST"])
def meta_rel_edit(rel_id: str | None = None):
    row = meta.get_meta_rel(rel_id) if rel_id else None
    facts = meta.list_meta_tables(kind="fact")
    dims = meta.list_meta_tables(kind="dim")
    if request.method == "POST":
        rid = request.form.get("id") or f"rel_{uuid.uuid4().hex[:8]}"
        lefts = request.form.getlist("join_left")
        rights = request.form.getlist("join_right")
        keys = []
        for i in range(max(len(lefts), len(rights))):
            l = (lefts[i] if i < len(lefts) else "").strip()
            r = (rights[i] if i < len(rights) else "").strip()
            if l and r:
                keys.append({"left": l, "right": r})
        try:
            meta.upsert_meta_rel(
                {
                    "id": rid,
                    "fact_table_id": request.form["fact_table_id"],
                    "dim_table_id": request.form["dim_table_id"],
                    "join_type": request.form.get("join_type") or "LEFT",
                    "join_keys_list": keys,
                    "enabled": 1 if request.form.get("enabled") else 0,
                    "remark": request.form.get("remark") or "",
                },
            )
            flash("维度关系已保存", "ok")
            return redirect(url_for("meta_rel_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            row = {
                "id": rid,
                "fact_table_id": request.form.get("fact_table_id"),
                "dim_table_id": request.form.get("dim_table_id"),
                "join_type": request.form.get("join_type") or "LEFT",
                "join_keys_list": keys,
                "enabled": 1 if request.form.get("enabled") else 0,
                "remark": request.form.get("remark") or "",
            }
    return render_template(
        "meta_rel_edit.html",
        row=row,
        facts=facts,
        dims=dims,
    )


@app.route("/meta/rels/match-keys", methods=["POST"])
def meta_rel_match_keys():
    payload = request.get_json(silent=True) or {}
    fact_id = payload.get("fact_table_id") or ""
    dim_id = payload.get("dim_table_id") or ""
    try:
        if not fact_id or not dim_id:
            raise ValueError("请先选择事实表与维度表")
        # ensure fields synced
        meta.sync_fields_from_physical(fact_id)
        meta.sync_fields_from_physical(dim_id)
        keys = meta.match_same_name_fields(fact_id, dim_id)
        return jsonify({"ok": True, "keys": keys})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/meta/rels/delete/<rel_id>", methods=["POST"])
def meta_rel_delete(rel_id: str):
    try:
        meta.delete_meta_rel(rel_id)
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("meta_rel_list"))


# ---------- Atomic ----------
@app.route("/metrics/atomic")
def atomic_list():
    q = (request.args.get("q") or "").strip()
    rows = []
    for r in db.list_atomic():
        if not _metric_kw_match(r, q, _ATOMIC_SEARCH_FIELDS):
            continue
        d = _metric_list_row(r)
        d["dim_labels"] = db.metric_bound_dim_labels("atomic", r["id"])
        rows.append(d)
    return render_template("atomic_list.html", rows=rows, q=q)


@app.route("/metrics/atomic/edit", methods=["GET", "POST"])
@app.route("/metrics/atomic/edit/<metric_id>", methods=["GET", "POST"])
def atomic_edit(metric_id: str | None = None):
    row = db.get_atomic(metric_id) if metric_id else None
    dims, selected_dims = _dim_form_context("atomic", metric_id)
    filter_text = metric_sql.row_filter_text(row) if row else ""
    if request.method == "POST":
        mid = request.form.get("id") or f"atom_{uuid.uuid4().hex[:8]}"
        dim_ids = request.form.getlist("dim_ids")
        try:
            filter_text = metric_sql.filter_from_form(request.form.get("filter_text"))
            sql_manual = 1 if request.form.get("sql_manual") else 0
            source_table = request.form["source_table"].strip()
            source_field = request.form["source_field"].strip()
            name_en = (request.form.get("name_en") or source_field).strip()
            agg_type = request.form.get("agg_type", "SUM")
            if sql_manual:
                sql_expr = (request.form.get("sql_expr") or "").strip()
                if not sql_expr:
                    raise ValueError("手工改写下 SQL 不能为空")
            else:
                sql_expr = metric_sql.build_atomic_sql_preview(
                    source_table=source_table,
                    source_field=source_field,
                    name_en=name_en,
                    filter_text=filter_text,
                )
            data = {
                "id": mid,
                "name": request.form["name"].strip(),
                "name_en": name_en,
                "source_table": source_table,
                "source_field": source_field,
                "agg_type": agg_type,
                "grain_dims": '["project_number"]',
                "remark": request.form.get("remark") or "",
                "filter_json": filter_text,
                "sql_expr": sql_expr,
                "sql_manual": sql_manual,
                "stage_type": (request.form.get("stage_type") or "").strip(),
                "rate_col": (request.form.get("rate_col") or "").strip(),
                "biz_line": (request.form.get("biz_line") or "").strip(),
                "theme_domain": (request.form.get("theme_domain") or "").strip(),
                "biz_object": (request.form.get("biz_object") or "").strip(),
                "biz_process": (request.form.get("biz_process") or "").strip(),
                "aliases": request.form.get("aliases") or "",
            }
            db.upsert_atomic(data, dim_ids=dim_ids)
            flash("原子指标已保存", "ok")
            return redirect(url_for("atomic_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            selected_dims = dim_ids
    return render_template(
        "atomic_edit.html",
        row=row,
        dims=dims,
        selected_dims=selected_dims,
        filter_text=filter_text,
        stages=db.STAGES,
        biz_catalog=biz_arch.taxonomy_catalog(),
    )


@app.route("/metrics/atomic/preview-sql", methods=["POST"])
def atomic_preview_sql():
    """Execute metric SQL for validation; returns JSON {ok, columns, rows, error, sql}."""
    payload = request.get_json(silent=True) or {}
    sql_raw = payload.get("sql") or request.form.get("sql") or ""
    try:
        sql = metric_sql.assert_executable_select(sql_raw)
        # wrap with limit for safety
        limited = f"SELECT * FROM ({sql}) AS _preview LIMIT 50"
        columns, rows = [], []
        with db.get_conn() as conn:
            cur = conn.execute(limited)
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        return jsonify(
            {
                "ok": True,
                "sql": sql,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows) >= 50,
            }
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e), "sql": sql_raw}), 400


@app.route("/metrics/atomic/delete/<metric_id>", methods=["POST"])
def atomic_delete(metric_id: str):
    try:
        db.delete_atomic(metric_id)
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("atomic_list"))


# ---------- Derived ----------
@app.route("/metrics/derived")
def derived_list():
    q = (request.args.get("q") or "").strip()
    rows = []
    for r in db.list_derived():
        if not _metric_kw_match(r, q, _DERIVED_SEARCH_FIELDS):
            continue
        d = _metric_list_row(r)
        d["dim_labels"] = db.metric_bound_dim_labels("derived", r["id"])
        rows.append(d)
    return render_template("derived_list.html", rows=rows, q=q)


@app.route("/metrics/derived/edit", methods=["GET", "POST"])
@app.route("/metrics/derived/edit/<metric_id>", methods=["GET", "POST"])
def derived_edit(metric_id: str | None = None):
    row = db.get_derived(metric_id) if metric_id else None
    from_atomic = (request.args.get("from_atomic") or "").strip() if not metric_id else ""
    prefill_atomic_id = ""
    prefill_name = ""
    seed_atom = db.get_atomic(from_atomic) if from_atomic else None
    if from_atomic and not seed_atom and not row:
        flash(f"来源原子不存在: {from_atomic}", "err")
    if seed_atom and not row:
        prefill_atomic_id = seed_atom["id"]
        stage = (seed_atom["stage_type"] or "").strip()
        prefill_name = f"{stage}总成本" if stage else (seed_atom["name"] or "")

    # Prefer amount atomics that already carry stage FX
    atomics = [
        a
        for a in db.list_atomic()
        if (a["rate_col"] or a["stage_type"])
        or (row and a["id"] == row["atomic_id"])
        or (prefill_atomic_id and a["id"] == prefill_atomic_id)
    ]
    if not atomics:
        atomics = list(db.list_atomic())
    dims, selected_dims = _dim_form_context("derived", metric_id)
    if seed_atom and not row and not metric_id:
        # Copy binds from source atomic for faster create
        atomic_dims = db.list_metric_dim_ids("atomic", seed_atom["id"])
        if atomic_dims:
            selected_dims = atomic_dims
    filter_text = metric_sql.row_filter_text(row) if row else ""
    if request.method == "POST":
        mid = request.form.get("id") or f"drv_{uuid.uuid4().hex[:8]}"
        dim_ids = request.form.getlist("dim_ids")
        try:
            filter_text = metric_sql.filter_from_form(request.form.get("filter_text"))
            data = {
                "id": mid,
                "name": request.form["name"].strip(),
                "atomic_id": request.form["atomic_id"],
                "stage_type": "",  # filled from atomic in upsert
                "amount_col": "",
                "rate_col": "",
                "grain_dims": '["project_number"]',
                "exposed": 1 if request.form.get("exposed") else 0,
                "filter_json": filter_text,
                "aliases": request.form.get("aliases") or "",
            }
            db.upsert_derived(data, dim_ids=dim_ids)
            flash("派生指标已保存", "ok")
            return redirect(url_for("derived_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            selected_dims = dim_ids
            prefill_atomic_id = request.form.get("atomic_id") or prefill_atomic_id
            prefill_name = request.form.get("name") or prefill_name
    return render_template(
        "derived_edit.html",
        row=row,
        atomics=atomics,
        dims=dims,
        selected_dims=selected_dims,
        filter_text=filter_text,
        currencies=db.list_currency_rules(),
        prefill_atomic_id=prefill_atomic_id,
        prefill_name=prefill_name,
        from_atomic=from_atomic,
    )


@app.route("/metrics/derived/preview-sql", methods=["POST"])
def derived_preview_sql():
    """Build final derived SQL (currency-converted) and optionally execute for validation."""
    payload = request.get_json(silent=True) or {}
    atomic_id = (payload.get("atomic_id") or "").strip()
    currency = (payload.get("currency") or "CNY").upper()
    execute = payload.get("execute", True)
    try:
        filter_text = metric_sql.filter_from_form(payload.get("filter_text"))
        if not atomic_id:
            raise ValueError("请选择来源原子指标")
        atomic = db.get_atomic(atomic_id)
        if not atomic:
            raise ValueError(f"原子指标不存在: {atomic_id}")
        rule = db.get_currency_rule(currency)
        if not rule:
            raise ValueError(f"不支持的币种: {currency}")
        amount_col = atomic["source_field"]
        rate_col = atomic["rate_col"] or ""
        if not amount_col or not rate_col:
            raise ValueError(
                f"原子指标未维护金额/汇率字段: {atomic['name']} "
                f"(source_field={amount_col!r}, rate_col={rate_col!r})"
            )
        # Prefer atomic English name / amount field as SQL alias (stable, IDENT-safe)
        alias = (atomic["name_en"] or "").strip() or amount_col
        sql = metric_sql.build_derived_sql_preview(
            source_table=atomic["source_table"],
            amount_col=amount_col,
            rate_col=rate_col,
            expr_template=rule["expr_template"],
            atomic_filter=metric_sql.row_filter_text(atomic),
            derived_filter=filter_text,
            alias=alias,
            currency=currency,
            sql_expr=atomic["sql_expr"] if "sql_expr" in atomic.keys() else None,
            sql_manual=bool(atomic["sql_manual"]) if "sql_manual" in atomic.keys() else False,
        )
        if not execute:
            return jsonify({"ok": True, "sql": sql, "currency": currency})

        sql = metric_sql.assert_executable_select(sql)
        limited = f"SELECT * FROM ({sql}) AS _preview LIMIT 50"
        columns, rows = [], []
        with db.get_conn() as conn:
            cur = conn.execute(limited)
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        return jsonify(
            {
                "ok": True,
                "sql": sql,
                "currency": currency,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows) >= 50,
            }
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/metrics/derived/delete/<metric_id>", methods=["POST"])
def derived_delete(metric_id: str):
    try:
        db.delete_derived(metric_id)
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("derived_list"))


# ---------- Composite ----------
@app.route("/metrics/composite")
def composite_list():
    q = (request.args.get("q") or "").strip()
    needle = q.lower()
    rows = []
    for r in db.list_composite():
        d = _metric_list_row(r)
        d["subs"] = json.loads(r["sub_metric_ids"] or "[]")
        d["dim_labels"] = db.metric_bound_dim_labels("composite", r["id"])
        if q:
            hit = _metric_kw_match(r, q, _COMPOSITE_SEARCH_FIELDS) or any(
                needle in str(s).lower() for s in d["subs"]
            )
            if not hit:
                continue
        rows.append(d)
    return render_template("composite_list.html", rows=rows, q=q)


@app.route("/metrics/composite/edit", methods=["GET", "POST"])
@app.route("/metrics/composite/edit/<metric_id>", methods=["GET", "POST"])
def composite_edit(metric_id: str | None = None):
    row = db.get_composite(metric_id) if metric_id else None
    from_derived = (
        (request.args.get("from_derived") or "").strip() if not metric_id else ""
    )
    seed_derived = db.get_derived(from_derived) if from_derived else None
    prefill_formula = ""
    prefill_name = ""
    if from_derived and not seed_derived and not row:
        flash(f"来源派生不存在: {from_derived}", "err")
    if seed_derived and not row:
        prefill_formula = seed_derived["name"] or ""
        prefill_name = f"{seed_derived['name']}复合" if seed_derived["name"] else ""

    # Prefer amount atomics (with stage FX); fall back to full list
    atomics = [
        a
        for a in db.list_atomic()
        if a["rate_col"] or a["stage_type"]
    ]
    if not atomics:
        atomics = list(db.list_atomic())
    derived = list(db.list_derived(exposed_only=True))
    # Ensure seed derived appears even if not exposed
    if seed_derived and not any(d["id"] == seed_derived["id"] for d in derived):
        derived = [seed_derived] + derived
    composites = [c for c in db.list_composite() if not row or c["id"] != row["id"]]
    selected = json.loads(row["sub_metric_ids"]) if row else []
    if seed_derived and not row and not selected:
        selected = [seed_derived["id"]]
    dims, selected_dims = _dim_form_context("composite", metric_id)
    if seed_derived and not row and not metric_id:
        derived_dims = db.list_metric_dim_ids("derived", seed_derived["id"])
        if derived_dims:
            selected_dims = derived_dims
    if request.method == "POST":
        mid = request.form.get("id") or f"cmp_{uuid.uuid4().hex[:8]}"
        subs = request.form.getlist("sub_metric_ids")
        dim_ids = request.form.getlist("dim_ids")
        data = {
            "id": mid,
            "name": request.form["name"].strip(),
            "formula": request.form["formula"].strip(),
            "sub_metric_ids": json.dumps(subs),
            "check_currency_same": 1 if request.form.get("check_currency_same") else 0,
            "check_granularity_same": 1
            if request.form.get("check_granularity_same")
            else 0,
            "biz_object": (request.form.get("biz_object") or "").strip(),
            "biz_process": (request.form.get("biz_process") or "").strip(),
            "aliases": request.form.get("aliases") or "",
        }
        try:
            db.upsert_composite(data, dim_ids=dim_ids)
            flash("复合指标已保存", "ok")
            return redirect(url_for("composite_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            selected_dims = dim_ids
            selected = subs
            prefill_formula = request.form.get("formula") or prefill_formula
            prefill_name = request.form.get("name") or prefill_name
    formula_for_tax = (row["formula"] if row else prefill_formula) or ""
    return render_template(
        "composite_edit.html",
        row=row,
        atomics=atomics,
        derived=derived,
        composites=composites,
        selected=selected,
        dims=dims,
        selected_dims=selected_dims,
        currencies=[c for c in db.list_currency_rules() if c["code"] != "USD"],
        taxonomy_defaults=db.taxonomy_from_formula(formula_for_tax, selected),
        biz_catalog=biz_arch.taxonomy_catalog(),
        from_derived=from_derived,
        prefill_formula=prefill_formula,
        prefill_name=prefill_name,
    )


@app.route("/metrics/composite/preview-sql", methods=["POST"])
def composite_preview_sql():
    """Build final composite SQL (formula expanded) and optionally execute for validation."""
    payload = request.get_json(silent=True) or {}
    formula = (payload.get("formula") or "").strip()
    sub_ids = payload.get("sub_metric_ids") or []
    if isinstance(sub_ids, str):
        sub_ids = [sub_ids] if sub_ids else []
    currency = (payload.get("currency") or "CNY").upper()
    if currency == "USD":
        return jsonify({"ok": False, "error": "复合指标预览不支持美元"}), 400
    execute = payload.get("execute", True)
    metric_id = (payload.get("id") or "preview").strip() or "preview"
    try:
        sql = build_composite_sql_preview(
            formula=formula,
            sub_metric_ids=list(sub_ids),
            currency=currency,
            check_currency_same=bool(payload.get("check_currency_same", True)),
            check_granularity_same=bool(payload.get("check_granularity_same", True)),
            metric_id=metric_id,
            alias=metric_id,
        )
        if not execute:
            return jsonify({"ok": True, "sql": sql, "currency": currency})

        sql = metric_sql.assert_executable_select(sql)
        limited = f"SELECT * FROM ({sql}) AS _preview LIMIT 50"
        columns, rows = [], []
        with db.get_conn() as conn:
            cur = conn.execute(limited)
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        return jsonify(
            {
                "ok": True,
                "sql": sql,
                "currency": currency,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows) >= 50,
            }
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/metrics/composite/delete/<metric_id>", methods=["POST"])
def composite_delete(metric_id: str):
    try:
        db.delete_composite(metric_id)
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("composite_list"))


@app.route("/metrics/map")
def metric_map_view():
    data = metric_map.build_metric_map()
    q = (request.args.get("q") or "").strip()
    return render_template(
        "metric_map.html",
        trees=data["trees"],
        orphans=data["orphans"],
        stats=data["stats"],
        q=q,
    )


@app.route("/metrics/map/panel/<metric_id>")
def metric_map_panel(metric_id: str):
    """HTML fragment: dependency subtree for overlay panel."""
    tree = metric_map.build_subtree(metric_id)
    if not tree:
        return f"指标不存在: {metric_id}", 404
    return render_template("metric_map_panel.html", tree=tree)


# ---------- Ask / Demo ----------
@app.route("/ask", methods=["GET", "POST"])
def ask():
    derived = db.list_derived(exposed_only=True)
    composites = db.list_composite()
    currencies = db.list_currency_rules()
    analysis_dims = meta.list_ask_analysis_fields()
    result = None
    form = {
        "mode": "form",
        "text": "所有项目的成本GAP以及其他相关的原子和派生指标，人民币",
        "metric_ids": ["cmp_cost_gap"],
        "currency": "CNY",
        "dims": [],
        "project_number": "",
        "region": "",
        "power_plant": "",
    }

    if request.method == "POST":
        form["mode"] = request.form.get("mode", "form")
        form["text"] = request.form.get("text", "")
        form["metric_ids"] = request.form.getlist("metric_ids")
        form["currency"] = request.form.get("currency", "CNY")
        form["dims"] = request.form.getlist("dims")
        form["project_number"] = request.form.get("project_number", "")
        form["region"] = request.form.get("region", "")
        form["power_plant"] = request.form.get("power_plant", "")

        if form["mode"] == "text":
            if not session.get("ask_session_id"):
                session["ask_session_id"] = uuid.uuid4().hex
            parsed = intent_mod.from_text(
                form["text"], session_id=session["ask_session_id"]
            )
            # ChatBI 多指标消歧 / 未识别意图能力引导：提示后不执行 SQL
            try:
                import askdata.intent.llm as intent_llm

                intent_dict_fn = intent_llm.intent_to_engine_dict
                needs_help = intent_llm.needs_capability_help
                help_result = intent_llm.capability_help_result
            except Exception:  # noqa: BLE001
                intent_dict_fn = None
                needs_help = None
                help_result = None

            if getattr(parsed, "disambiguate_msg", None):
                if intent_dict_fn is not None:
                    intent_dict = intent_dict_fn(parsed)
                else:
                    intent_dict = {
                        "metric_ids": parsed.metric_ids,
                        "currency": parsed.currency,
                        "dims": parsed.dims,
                        "filters": parsed.filters,
                        "raw_text": parsed.raw_text,
                        "source": parsed.source or "",
                        "intent_type": parsed.intent_type,
                        "include_related": parsed.include_related,
                        "calc_instruction": parsed.calc_instruction,
                        "metric_names": list(parsed.metric_names or []),
                        "related_metric_ids": list(parsed.related_metric_ids or []),
                        "payload_zh": dict(parsed.payload_zh or {}),
                    }
                result = EngineResult(
                    ok=False,
                    intent=intent_dict,
                    error=f"需要消歧：{parsed.disambiguate_msg}",
                )
            elif needs_help is not None and needs_help(parsed):
                result = help_result(parsed)
            else:
                result = None
        else:
            parsed = intent_mod.from_form(
                metric_ids=form["metric_ids"],
                currency=form["currency"],
                dims=form["dims"],
                project_number=form["project_number"],
                region=form["region"],
                power_plant=form["power_plant"],
                raw_text=form["text"],
            )
            result = None

        if result is None:
            # Non-query intents: dictionary / definition — no SQL
            try:
                import askdata.intent.llm as intent_llm

                non_query = intent_llm.handle_non_query(parsed)
            except Exception:  # noqa: BLE001
                non_query = None
            if non_query is not None:
                result = non_query
            else:
                result = run(parsed)

    intent_view = display.intent_zh(result.intent) if result else None
    audit_view = display.audit_zh(result.audit) if result and result.ok else None
    col_headers = (
        [display.column_zh(c) for c in result.columns] if result and result.ok else []
    )

    return render_template(
        "ask.html",
        derived=derived,
        composites=composites,
        currencies=currencies,
        analysis_dims=analysis_dims,
        form=form,
        result=result,
        intent_view=intent_view,
        audit_view=audit_view,
        col_headers=col_headers,
    )


@app.route("/admin/reset", methods=["POST"])
def reset_db():
    db.init_db(force=True)
    flash("已重建数据库并重新灌种", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    import os

    db.init_db()
    host = os.environ.get("ASKDATA_HOST", "0.0.0.0")
    port = int(os.environ.get("ASKDATA_PORT", "5050"))
    debug = os.environ.get("ASKDATA_DEBUG", "1") == "1"
    # Scripts disable reloader so stop/restart can track a single PID.
    use_reloader = os.environ.get("ASKDATA_RELOAD", "0") == "1"
    app.run(host=host, port=port, debug=debug, use_reloader=use_reloader)

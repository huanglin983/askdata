"""Flask app: metric config CRUD + Text-to-SQL demo."""
from __future__ import annotations

import json
import uuid

from flask import Flask, flash, redirect, render_template, request, url_for

import db
import display
import intent as intent_mod
from engine import run

app = Flask(__name__)
app.secret_key = "metric-t2sql-demo-dev"


@app.template_filter("zh_json")
def zh_json_filter(value):
    return display.dumps_zh(value)


@app.before_request
def _ensure_db():
    db.init_db()


def _dim_form_context(metric_type: str | None = None, metric_id: str | None = None):
    dims = db.list_analysis_dims(enabled_only=False)
    selected = (
        db.list_metric_dim_ids(metric_type, metric_id)
        if metric_type and metric_id
        else []
    )
    # new metric default: all enabled dims
    if not selected and not metric_id:
        selected = [d["id"] for d in dims if d["enabled"]]
    return dims, selected


@app.route("/")
def index():
    return render_template(
        "index.html",
        atomic_n=len(db.list_atomic()),
        derived_n=len(db.list_derived()),
        composite_n=len(db.list_composite()),
        dim_n=len(db.list_analysis_dims()),
    )


# ---------- Analysis dimensions ----------
@app.route("/dims")
def dim_list():
    return render_template("dim_list.html", rows=db.list_analysis_dims())


@app.route("/dims/edit", methods=["GET", "POST"])
@app.route("/dims/edit/<dim_id>", methods=["GET", "POST"])
def dim_edit(dim_id: str | None = None):
    row = db.get_analysis_dim(dim_id) if dim_id else None
    if request.method == "POST":
        did = request.form.get("id") or f"dim_{uuid.uuid4().hex[:8]}"
        data = {
            "id": did,
            "code": request.form["code"].strip(),
            "name": request.form["name"].strip(),
            "source_table": request.form["source_table"].strip(),
            "source_field": request.form["source_field"].strip(),
            "dim_role": request.form.get("dim_role", "attr"),
            "enabled": 1 if request.form.get("enabled") else 0,
            "sort_no": int(request.form.get("sort_no") or 100),
            "remark": request.form.get("remark") or "",
        }
        try:
            db.upsert_analysis_dim(data)
            flash("分析维度已保存", "ok")
            return redirect(url_for("dim_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
    return render_template("dim_edit.html", row=row)


@app.route("/dims/delete/<dim_id>", methods=["POST"])
def dim_delete(dim_id: str):
    try:
        db.delete_analysis_dim(dim_id)
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("dim_list"))


# ---------- Atomic ----------
@app.route("/metrics/atomic")
def atomic_list():
    rows = []
    for r in db.list_atomic():
        d = dict(r)
        d["dim_labels"] = db.metric_bound_dim_labels("atomic", r["id"])
        rows.append(d)
    return render_template("atomic_list.html", rows=rows)


@app.route("/metrics/atomic/edit", methods=["GET", "POST"])
@app.route("/metrics/atomic/edit/<metric_id>", methods=["GET", "POST"])
def atomic_edit(metric_id: str | None = None):
    row = db.get_atomic(metric_id) if metric_id else None
    dims, selected_dims = _dim_form_context("atomic", metric_id)
    if request.method == "POST":
        mid = request.form.get("id") or f"atom_{uuid.uuid4().hex[:8]}"
        dim_ids = request.form.getlist("dim_ids")
        data = {
            "id": mid,
            "name": request.form["name"].strip(),
            "source_table": request.form["source_table"].strip(),
            "source_field": request.form["source_field"].strip(),
            "agg_type": request.form.get("agg_type", "SUM"),
            "grain_dims": '["project_number"]',
            "remark": request.form.get("remark") or "",
        }
        try:
            db.upsert_atomic(data, dim_ids=dim_ids)
            flash("原子指标已保存", "ok")
            return redirect(url_for("atomic_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            selected_dims = dim_ids
    return render_template(
        "atomic_edit.html", row=row, dims=dims, selected_dims=selected_dims
    )


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
    rows = []
    for r in db.list_derived():
        d = dict(r)
        d["dim_labels"] = db.metric_bound_dim_labels("derived", r["id"])
        rows.append(d)
    return render_template("derived_list.html", rows=rows, stages=db.STAGES)


@app.route("/metrics/derived/edit", methods=["GET", "POST"])
@app.route("/metrics/derived/edit/<metric_id>", methods=["GET", "POST"])
def derived_edit(metric_id: str | None = None):
    row = db.get_derived(metric_id) if metric_id else None
    atomics = db.list_atomic()
    stage_map = db.stage_map()
    dims, selected_dims = _dim_form_context("derived", metric_id)
    if request.method == "POST":
        mid = request.form.get("id") or f"drv_{uuid.uuid4().hex[:8]}"
        stage = request.form["stage_type"]
        amount_col = request.form.get("amount_col") or stage_map.get(stage, ("", ""))[0]
        rate_col = request.form.get("rate_col") or stage_map.get(stage, ("", ""))[1]
        dim_ids = request.form.getlist("dim_ids")
        data = {
            "id": mid,
            "name": request.form["name"].strip(),
            "atomic_id": request.form["atomic_id"],
            "stage_type": stage,
            "amount_col": amount_col,
            "rate_col": rate_col,
            "grain_dims": '["project_number"]',
            "exposed": 1 if request.form.get("exposed") else 0,
        }
        try:
            db.upsert_derived(data, dim_ids=dim_ids)
            flash("派生指标已保存", "ok")
            return redirect(url_for("derived_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            selected_dims = dim_ids
    return render_template(
        "derived_edit.html",
        row=row,
        atomics=atomics,
        stages=db.STAGES,
        stage_map=stage_map,
        dims=dims,
        selected_dims=selected_dims,
    )


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
    rows = []
    for r in db.list_composite():
        d = dict(r)
        d["subs"] = json.loads(r["sub_metric_ids"] or "[]")
        d["dim_labels"] = db.metric_bound_dim_labels("composite", r["id"])
        rows.append(d)
    return render_template("composite_list.html", rows=rows)


@app.route("/metrics/composite/edit", methods=["GET", "POST"])
@app.route("/metrics/composite/edit/<metric_id>", methods=["GET", "POST"])
def composite_edit(metric_id: str | None = None):
    row = db.get_composite(metric_id) if metric_id else None
    derived = db.list_derived(exposed_only=True)
    composites = [c for c in db.list_composite() if not row or c["id"] != row["id"]]
    selected = json.loads(row["sub_metric_ids"]) if row else []
    dims, selected_dims = _dim_form_context("composite", metric_id)
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
        }
        try:
            db.upsert_composite(data, dim_ids=dim_ids)
            flash("复合指标已保存", "ok")
            return redirect(url_for("composite_list"))
        except Exception as e:  # noqa: BLE001
            flash(str(e), "err")
            selected_dims = dim_ids
            selected = subs
    return render_template(
        "composite_edit.html",
        row=row,
        derived=derived,
        composites=composites,
        selected=selected,
        dims=dims,
        selected_dims=selected_dims,
    )


@app.route("/metrics/composite/delete/<metric_id>", methods=["POST"])
def composite_delete(metric_id: str):
    try:
        db.delete_composite(metric_id)
        flash("已删除", "ok")
    except Exception as e:  # noqa: BLE001
        flash(str(e), "err")
    return redirect(url_for("composite_list"))


# ---------- Ask / Demo ----------
@app.route("/ask", methods=["GET", "POST"])
def ask():
    derived = db.list_derived(exposed_only=True)
    composites = db.list_composite()
    currencies = db.list_currency_rules()
    analysis_dims = [
        d for d in db.list_analysis_dims(enabled_only=True) if d["dim_role"] == "attr"
    ]
    result = None
    form = {
        "mode": "form",
        "text": "查询项目P001的成本GAP，人民币，带电站",
        "metric_ids": ["cmp_cost_gap"],
        "currency": "CNY",
        "dims": ["power_plant"],
        "project_number": "P001",
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
            parsed = intent_mod.from_text(form["text"])
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
    db.init_db()
    app.run(host="127.0.0.1", port=5050, debug=True)

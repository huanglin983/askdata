"""Warehouse metadata: tables, fields, fact→dim relationships."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

import db

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

FIELD_CN = {
    "project_number": "项目编号",
    "project_name": "项目名称",
    "power_plant": "电站",
    "region": "区域",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta_table (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            physical_name TEXT NOT NULL UNIQUE,
            table_kind TEXT NOT NULL DEFAULT 'dim',
            enabled INTEGER NOT NULL DEFAULT 1,
            remark TEXT,
            grain_keys TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS meta_field (
            id TEXT PRIMARY KEY,
            table_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            data_type TEXT,
            is_pk INTEGER NOT NULL DEFAULT 0,
            semantic_role TEXT NOT NULL DEFAULT 'other',
            is_analysis_dim INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_no INTEGER NOT NULL DEFAULT 100,
            UNIQUE(table_id, field_name),
            FOREIGN KEY (table_id) REFERENCES meta_table(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS meta_table_rel (
            id TEXT PRIMARY KEY,
            fact_table_id TEXT NOT NULL,
            dim_table_id TEXT NOT NULL,
            join_type TEXT NOT NULL DEFAULT 'LEFT',
            join_keys TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            auto_sync_dims INTEGER NOT NULL DEFAULT 1,
            remark TEXT,
            UNIQUE(fact_table_id, dim_table_id),
            FOREIGN KEY (fact_table_id) REFERENCES meta_table(id),
            FOREIGN KEY (dim_table_id) REFERENCES meta_table(id)
        );
        """
    )
    # migrate existing DBs
    cols = {r[1] for r in conn.execute("PRAGMA table_info(meta_field)")}
    if "is_analysis_dim" not in cols:
        conn.execute(
            "ALTER TABLE meta_field ADD COLUMN is_analysis_dim INTEGER NOT NULL DEFAULT 1"
        )
        conn.execute(
            """UPDATE meta_field SET is_analysis_dim=0
               WHERE semantic_role IN ('measure','rate')"""
        )
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(meta_table)")}
    if "grain_keys" not in tcols:
        conn.execute(
            "ALTER TABLE meta_table ADD COLUMN grain_keys TEXT NOT NULL DEFAULT '[]'"
        )
    # backfill grain_keys from field semantic_role when empty
    for t in conn.execute("SELECT id, grain_keys FROM meta_table"):
        raw = (t["grain_keys"] or "").strip()
        if raw and raw != "[]":
            continue
        names = [
            r["field_name"]
            for r in conn.execute(
                """SELECT field_name FROM meta_field
                   WHERE table_id=? AND enabled=1 AND semantic_role='grain'
                   ORDER BY is_pk DESC, sort_no, field_name""",
                (t["id"],),
            )
        ]
        if names:
            conn.execute(
                "UPDATE meta_table SET grain_keys=? WHERE id=?",
                (json.dumps(names, ensure_ascii=False), t["id"]),
            )


def seed_demo_meta(conn: sqlite3.Connection) -> None:
    """Register demo fact/dim tables, fields, and relationship."""
    n = conn.execute("SELECT COUNT(*) AS c FROM meta_table").fetchone()["c"]
    if n:
        return

    fact_id = "tbl_fact_cost"
    dim_id = "tbl_dim_project"
    conn.executemany(
        """INSERT INTO meta_table(id, name, physical_name, table_kind, enabled, remark, grain_keys)
           VALUES (?,?,?,?,?,?,?)""",
        [
            (
                fact_id,
                "成本对比宽表",
                "ads_fin_tracker_comparison_summary_df",
                "fact",
                1,
                "五阶段金额与汇率事实表",
                json.dumps(["project_number"], ensure_ascii=False),
            ),
            (
                dim_id,
                "项目维度",
                "dim_project",
                "dim",
                1,
                "项目属性维度表",
                json.dumps(["project_number"], ensure_ascii=False),
            ),
        ],
    )
    _sync_fields_conn(conn, fact_id)
    _sync_fields_conn(conn, dim_id)
    # mark semantic roles
    conn.execute(
        """UPDATE meta_field SET semantic_role='grain', is_pk=1, display_name=?
           WHERE table_id=? AND field_name='project_number'""",
        (FIELD_CN["project_number"], fact_id),
    )
    conn.execute(
        """UPDATE meta_field SET semantic_role='grain', is_pk=1, display_name=?
           WHERE table_id=? AND field_name='project_number'""",
        (FIELD_CN["project_number"], dim_id),
    )
    for fname, role in (
        ("project_name", "attr"),
        ("power_plant", "attr"),
        ("region", "attr"),
    ):
        conn.execute(
            """UPDATE meta_field SET semantic_role=?, display_name=?
               WHERE table_id=? AND field_name=?""",
            (role, FIELD_CN.get(fname, fname), dim_id, fname),
        )
    for stage, amount, rate in db.STAGES:
        conn.execute(
            """UPDATE meta_field SET semantic_role='measure', display_name=?
               WHERE table_id=? AND field_name=?""",
            (f"{stage}金额", fact_id, amount),
        )
        conn.execute(
            """UPDATE meta_field SET semantic_role='rate', display_name=?
               WHERE table_id=? AND field_name=?""",
            (f"{stage}汇率", fact_id, rate),
        )

    join_keys = json.dumps(
        [{"left": "project_number", "right": "project_number"}], ensure_ascii=False
    )
    conn.execute(
        """INSERT INTO meta_table_rel
           (id, fact_table_id, dim_table_id, join_type, join_keys, enabled, auto_sync_dims, remark)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            "rel_fact_dim_project",
            fact_id,
            dim_id,
            "LEFT",
            join_keys,
            1,
            1,
            "事实表按项目编号关联项目维度",
        ),
    )
    # measure/rate fields are not used as analysis dimensions
    conn.execute(
        """UPDATE meta_field SET is_analysis_dim=0
           WHERE table_id=? AND semantic_role IN ('measure','rate')""",
        (fact_id,),
    )


def list_physical_tables() -> list[str]:
    """User tables in SQLite demo DB (exclude meta/metric catalog)."""
    skip = {
        "meta_table",
        "meta_field",
        "meta_table_rel",
        "biz_arch",
        "metric_atomic",
        "metric_derived",
        "metric_composite",
        "metric_dim_bind",
        "currency_rule",
        "analysis_dim",
        "sqlite_sequence",
    }
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows if r["name"] not in skip]


def list_meta_tables(kind: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM meta_table"
    params: list[Any] = []
    if kind:
        sql += " WHERE table_kind=?"
        params.append(kind)
    sql += " ORDER BY table_kind, name"
    with db.get_conn() as conn:
        return list(conn.execute(sql, params))


def get_meta_table(table_id: str) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM meta_table WHERE id=?", (table_id,)
        ).fetchone()


def get_meta_table_by_physical(physical_name: str) -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM meta_table WHERE physical_name=?", (physical_name,)
        ).fetchone()


def get_primary_fact_table() -> sqlite3.Row | None:
    with db.get_conn() as conn:
        return conn.execute(
            """SELECT * FROM meta_table
               WHERE table_kind='fact' AND enabled=1
               ORDER BY name LIMIT 1"""
        ).fetchone()


def parse_grain_keys(raw: Any) -> list[str]:
    """Normalize grain_keys from JSON text / list / form values."""
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else [text]
        except json.JSONDecodeError:
            items = [p.strip() for p in text.split(",") if p.strip()]
    else:
        return []
    out: list[str] = []
    for x in items:
        name = str(x or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def get_table_grain_keys(table_id: str) -> list[str]:
    with db.get_conn() as conn:
        return _get_table_grain_keys_conn(conn, table_id)


def _get_table_grain_keys_conn(conn: sqlite3.Connection, table_id: str) -> list[str]:
    row = conn.execute(
        "SELECT grain_keys FROM meta_table WHERE id=?", (table_id,)
    ).fetchone()
    if not row:
        return []
    keys = parse_grain_keys(row["grain_keys"] if "grain_keys" in row.keys() else "[]")
    if keys:
        return keys
    # fallback: field-level grain roles
    return [
        r["field_name"]
        for r in conn.execute(
            """SELECT field_name FROM meta_field
               WHERE table_id=? AND enabled=1 AND semantic_role='grain'
               ORDER BY is_pk DESC, sort_no, field_name""",
            (table_id,),
        )
    ]


def _apply_grain_keys(
    conn: sqlite3.Connection, table_id: str, field_names: list[str]
) -> list[str]:
    """Persist ordered business keys and sync field semantic_role='grain'."""
    fields = list(
        conn.execute(
            """SELECT id, field_name, semantic_role, sort_no, is_pk
               FROM meta_field WHERE table_id=?""",
            (table_id,),
        )
    )
    by_name = {f["field_name"]: f for f in fields}
    selected = parse_grain_keys(field_names)
    for name in selected:
        if name not in by_name:
            raise ValueError(f"业务主键字段不属于本表: {name}")
    # order by field sort_no (then is_pk, name) among selected
    cleaned = sorted(
        selected,
        key=lambda n: (
            int(by_name[n]["sort_no"] or 100),
            -int(by_name[n]["is_pk"] or 0),
            n,
        ),
    )
    conn.execute(
        "UPDATE meta_table SET grain_keys=? WHERE id=?",
        (json.dumps(cleaned, ensure_ascii=False), table_id),
    )
    grain_set = set(cleaned)
    for f in fields:
        if f["field_name"] in grain_set:
            if f["semantic_role"] != "grain":
                conn.execute(
                    "UPDATE meta_field SET semantic_role='grain' WHERE id=?",
                    (f["id"],),
                )
        elif f["semantic_role"] == "grain":
            conn.execute(
                "UPDATE meta_field SET semantic_role='attr' WHERE id=?",
                (f["id"],),
            )
    return cleaned


def _rebuild_grain_keys_from_fields(conn: sqlite3.Connection, table_id: str) -> None:
    """When field roles change manually, refresh table grain_keys order."""
    names = [
        r["field_name"]
        for r in conn.execute(
            """SELECT field_name FROM meta_field
               WHERE table_id=? AND enabled=1 AND semantic_role='grain'
               ORDER BY is_pk DESC, sort_no, field_name""",
            (table_id,),
        )
    ]
    conn.execute(
        "UPDATE meta_table SET grain_keys=? WHERE id=?",
        (json.dumps(names, ensure_ascii=False), table_id),
    )


def _auto_grain_from_pk(conn: sqlite3.Connection, table_id: str) -> None:
    """If table has no grain_keys yet, use physical PK fields as business keys."""
    existing = parse_grain_keys(
        (
            conn.execute(
                "SELECT grain_keys FROM meta_table WHERE id=?", (table_id,)
            ).fetchone()
            or {"grain_keys": "[]"}
        )["grain_keys"]
    )
    if existing:
        _apply_grain_keys(conn, table_id, existing)
        return
    pks = [
        r["field_name"]
        for r in conn.execute(
            """SELECT field_name FROM meta_field
               WHERE table_id=? AND is_pk=1
               ORDER BY sort_no, field_name""",
            (table_id,),
        )
    ]
    if pks:
        _apply_grain_keys(conn, table_id, pks)


def upsert_meta_table(
    data: dict, sync_fields: bool = True, grain_keys: list[str] | None = None
) -> str:
    tid = data["id"]
    physical = data["physical_name"].strip()
    if not _IDENT.match(physical):
        raise ValueError(f"非法物理表名: {physical}")
    kind = data.get("table_kind") or "dim"
    if kind not in ("fact", "dim"):
        raise ValueError("table_kind 只能是 fact 或 dim")
    with db.get_conn() as conn:
        # ensure physical exists in sqlite for demo
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (physical,),
        ).fetchone()
        if not exists:
            raise ValueError(f"物理表不存在: {physical}（请先在库中建表或选已有表）")
        # preserve existing grain_keys unless explicitly provided
        prev = conn.execute(
            "SELECT grain_keys FROM meta_table WHERE id=?", (tid,)
        ).fetchone()
        prev_keys = parse_grain_keys(prev["grain_keys"]) if prev else []
        keys_json = json.dumps(
            parse_grain_keys(grain_keys) if grain_keys is not None else prev_keys,
            ensure_ascii=False,
        )
        conn.execute(
            """INSERT INTO meta_table(id, name, physical_name, table_kind, enabled, remark, grain_keys)
               VALUES (:id,:name,:physical_name,:table_kind,:enabled,:remark,:grain_keys)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 physical_name=excluded.physical_name,
                 table_kind=excluded.table_kind,
                 enabled=excluded.enabled,
                 remark=excluded.remark,
                 grain_keys=excluded.grain_keys""",
            {
                "id": tid,
                "name": data["name"].strip(),
                "physical_name": physical,
                "table_kind": kind,
                "enabled": int(data.get("enabled", 1)),
                "remark": data.get("remark") or "",
                "grain_keys": keys_json,
            },
        )
        if sync_fields:
            _sync_fields_conn(conn, tid)
        if grain_keys is not None:
            _apply_grain_keys(conn, tid, grain_keys)
        else:
            _auto_grain_from_pk(conn, tid)
        conn.commit()
    return tid


def set_table_grain_keys(table_id: str, field_names: list[str]) -> list[str]:
    """Configure ordered business primary keys (数据粒度) for a table."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM meta_table WHERE id=?", (table_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"表元数据不存在: {table_id}")
        cleaned = _apply_grain_keys(conn, table_id, field_names)
        conn.commit()
    return cleaned


def delete_meta_table(table_id: str) -> None:
    with db.get_conn() as conn:
        used = conn.execute(
            """SELECT COUNT(*) AS c FROM meta_table_rel
               WHERE fact_table_id=? OR dim_table_id=?""",
            (table_id, table_id),
        ).fetchone()["c"]
        if used:
            raise ValueError("表仍被维度关系引用，无法删除")
        conn.execute("DELETE FROM meta_field WHERE table_id=?", (table_id,))
        conn.execute("DELETE FROM meta_table WHERE id=?", (table_id,))
        conn.commit()


def list_meta_fields(table_id: str) -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        return list(
            conn.execute(
                """SELECT * FROM meta_field WHERE table_id=?
                   ORDER BY sort_no, field_name""",
                (table_id,),
            )
        )


def sync_fields_from_physical(table_id: str) -> int:
    with db.get_conn() as conn:
        n = _sync_fields_conn(conn, table_id)
        _auto_grain_from_pk(conn, table_id)
        conn.commit()
        return n


def _sync_fields_conn(conn: sqlite3.Connection, table_id: str) -> int:
    row = conn.execute(
        "SELECT physical_name FROM meta_table WHERE id=?", (table_id,)
    ).fetchone()
    if not row:
        raise ValueError("元数据表不存在")
    physical = row["physical_name"]
    cols = list(conn.execute(f"PRAGMA table_info({physical})"))
    added = 0
    for i, c in enumerate(cols):
        fname = c[1]
        is_pk = 1 if c[5] else 0
        dtype = c[2] or ""
        existing = conn.execute(
            "SELECT id FROM meta_field WHERE table_id=? AND field_name=?",
            (table_id, fname),
        ).fetchone()
        if existing:
            continue
        # new fields: physical PK hints grain only when table has no grain yet
        role = "other"
        display = FIELD_CN.get(fname, fname)
        conn.execute(
            """INSERT INTO meta_field
               (id, table_id, field_name, display_name, data_type, is_pk,
                semantic_role, is_analysis_dim, enabled, sort_no)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                f"fld_{uuid.uuid4().hex[:10]}",
                table_id,
                fname,
                display,
                dtype,
                is_pk,
                role,
                1,  # default: usable as analysis dimension
                1,
                (i + 1) * 10,
            ),
        )
        added += 1
    return added


def update_meta_field(field_id: str, data: dict) -> None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT table_id FROM meta_field WHERE id=?", (field_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"字段不存在: {field_id}")
        conn.execute(
            """UPDATE meta_field SET
                 display_name=?,
                 semantic_role=?,
                 is_analysis_dim=?,
                 enabled=?,
                 sort_no=?
               WHERE id=?""",
            (
                data["display_name"].strip(),
                data.get("semantic_role") or "other",
                int(data.get("is_analysis_dim", 1)),
                int(data.get("enabled", 1)),
                int(data.get("sort_no") or 100),
                field_id,
            ),
        )
        _rebuild_grain_keys_from_fields(conn, row["table_id"])
        conn.commit()


def list_meta_rels() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*,
                      f.name AS fact_name, f.physical_name AS fact_physical,
                      d.name AS dim_name, d.physical_name AS dim_physical
               FROM meta_table_rel r
               JOIN meta_table f ON f.id = r.fact_table_id
               JOIN meta_table d ON d.id = r.dim_table_id
               ORDER BY f.name, d.name"""
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["join_keys_list"] = json.loads(r["join_keys"] or "[]")
            out.append(item)
        return out


def get_meta_rel(rel_id: str) -> dict | None:
    with db.get_conn() as conn:
        r = conn.execute(
            """SELECT r.*,
                      f.name AS fact_name, f.physical_name AS fact_physical,
                      d.name AS dim_name, d.physical_name AS dim_physical
               FROM meta_table_rel r
               JOIN meta_table f ON f.id = r.fact_table_id
               JOIN meta_table d ON d.id = r.dim_table_id
               WHERE r.id=?""",
            (rel_id,),
        ).fetchone()
        if not r:
            return None
        item = dict(r)
        item["join_keys_list"] = json.loads(r["join_keys"] or "[]")
        return item


def match_same_name_fields(fact_table_id: str, dim_table_id: str) -> list[dict[str, str]]:
    """Auto-detect join keys: fields with the same name on fact and dim."""
    fact_fields = {f["field_name"] for f in list_meta_fields(fact_table_id)}
    dim_fields = {f["field_name"] for f in list_meta_fields(dim_table_id)}
    common = sorted(fact_fields & dim_fields)
    return [{"left": n, "right": n} for n in common]


def upsert_meta_rel(data: dict) -> str:
    rid = data["id"]
    keys = data.get("join_keys_list") or []
    if isinstance(keys, str):
        keys = json.loads(keys)
    if not keys:
        raise ValueError("至少配置一组关联字段")
    for k in keys:
        if not _IDENT.match(k.get("left", "")) or not _IDENT.match(k.get("right", "")):
            raise ValueError("关联字段名非法")
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO meta_table_rel
               (id, fact_table_id, dim_table_id, join_type, join_keys,
                enabled, auto_sync_dims, remark)
               VALUES (:id,:fact_table_id,:dim_table_id,:join_type,:join_keys,
                       :enabled,:auto_sync_dims,:remark)
               ON CONFLICT(id) DO UPDATE SET
                 fact_table_id=excluded.fact_table_id,
                 dim_table_id=excluded.dim_table_id,
                 join_type=excluded.join_type,
                 join_keys=excluded.join_keys,
                 enabled=excluded.enabled,
                 auto_sync_dims=excluded.auto_sync_dims,
                 remark=excluded.remark""",
            {
                "id": rid,
                "fact_table_id": data["fact_table_id"],
                "dim_table_id": data["dim_table_id"],
                "join_type": data.get("join_type") or "LEFT",
                "join_keys": json.dumps(keys, ensure_ascii=False),
                "enabled": int(data.get("enabled", 1)),
                "auto_sync_dims": 0,
                "remark": data.get("remark") or "",
            },
        )
        conn.commit()
    return rid


def delete_meta_rel(rel_id: str) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM meta_table_rel WHERE id=?", (rel_id,))
        conn.commit()


def _field_row_to_dim(row: sqlite3.Row | dict) -> dict:
    """Normalize a joined meta_field+meta_table row into an analysis-dim dict."""
    r = dict(row)
    kind = r.get("table_kind") or "dim"
    return {
        "id": r["id"],
        "code": r["field_name"],
        "name": r["display_name"] or r["field_name"],
        "source_table": r["physical_name"],
        "source_field": r["field_name"],
        "dim_role": "grain" if r.get("semantic_role") == "grain" else "attr",
        "semantic_role": r.get("semantic_role") or "other",
        "enabled": r.get("enabled", 1),
        "sort_no": r.get("sort_no", 100),
        "origin": kind,  # fact | dim
        "table_kind": kind,
        "table_name": r.get("table_name") or r.get("physical_name"),
        "table_id": r.get("table_id"),
        "is_analysis_dim": r.get("is_analysis_dim", 1),
    }


def list_bindable_meta_fields() -> list[dict]:
    """Fields that can be bound to metrics (is_analysis_dim, exclude measure/rate)."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT f.*, t.physical_name, t.table_kind, t.name AS table_name
               FROM meta_field f
               JOIN meta_table t ON t.id = f.table_id
               WHERE t.enabled=1 AND f.enabled=1 AND f.is_analysis_dim=1
                 AND f.semantic_role NOT IN ('measure','rate')
               ORDER BY t.table_kind, t.name, f.sort_no, f.field_name"""
        ).fetchall()
    return [_field_row_to_dim(r) for r in rows]


def list_ask_analysis_fields() -> list[dict]:
    """Ask-time analysis dims: fact + related dim fields flagged is_analysis_dim.

    Excludes grain (always in SELECT) and measure/rate.
    """
    fact = get_primary_fact_table()
    if not fact:
        return []
    with db.get_conn() as conn:
        table_ids = {fact["id"]}
        for rel in conn.execute(
            """SELECT dim_table_id FROM meta_table_rel
               WHERE enabled=1 AND fact_table_id=?""",
            (fact["id"],),
        ):
            table_ids.add(rel["dim_table_id"])

        placeholders = ",".join("?" * len(table_ids))
        rows = conn.execute(
            f"""SELECT f.*, t.physical_name, t.table_kind, t.name AS table_name
                FROM meta_field f
                JOIN meta_table t ON t.id = f.table_id
                WHERE f.table_id IN ({placeholders})
                  AND t.enabled=1 AND f.enabled=1 AND f.is_analysis_dim=1
                  AND f.semantic_role NOT IN ('grain','measure','rate')
                ORDER BY CASE t.table_kind WHEN 'fact' THEN 0 ELSE 1 END,
                         t.name, f.sort_no, f.field_name""",
            tuple(table_ids),
        ).fetchall()

    # dedupe by field_name: prefer dim over fact when both exist
    by_code: dict[str, dict] = {}
    for r in rows:
        item = _field_row_to_dim(r)
        code = item["code"]
        if code not in by_code or item["origin"] == "dim":
            by_code[code] = item
    return list(by_code.values())


def get_analysis_field_by_code(code: str) -> dict | None:
    """Resolve analysis field by field_name (or physical.field_name)."""
    if not code:
        return None
    physical = None
    fname = code
    if "." in code:
        physical, fname = code.split(".", 1)

    with db.get_conn() as conn:
        if physical:
            row = conn.execute(
                """SELECT f.*, t.physical_name, t.table_kind, t.name AS table_name
                   FROM meta_field f
                   JOIN meta_table t ON t.id = f.table_id
                   WHERE t.physical_name=? AND f.field_name=?
                     AND f.enabled=1 AND f.is_analysis_dim=1""",
                (physical, fname),
            ).fetchone()
            return _field_row_to_dim(row) if row else None

        rows = conn.execute(
            """SELECT f.*, t.physical_name, t.table_kind, t.name AS table_name
               FROM meta_field f
               JOIN meta_table t ON t.id = f.table_id
               WHERE f.field_name=? AND t.enabled=1 AND f.enabled=1
                 AND f.is_analysis_dim=1
               ORDER BY CASE t.table_kind WHEN 'dim' THEN 0 ELSE 1 END,
                        f.sort_no""",
            (fname,),
        ).fetchall()
    if not rows:
        return None
    return _field_row_to_dim(rows[0])


def get_fact_grain_field() -> str:
    """Primary grain column on the fact table (first business key)."""
    fact = get_primary_fact_table()
    if not fact:
        return "project_number"
    keys = get_table_grain_keys(fact["id"])
    if keys:
        return keys[0]
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT field_name FROM meta_field
               WHERE table_id=? AND enabled=1 AND semantic_role='grain'
               ORDER BY is_pk DESC, sort_no LIMIT 1""",
            (fact["id"],),
        ).fetchone()
    return row["field_name"] if row else "project_number"


def get_meta_field(field_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT f.*, t.physical_name, t.table_kind, t.name AS table_name
               FROM meta_field f
               JOIN meta_table t ON t.id = f.table_id
               WHERE f.id=?""",
            (field_id,),
        ).fetchone()
    return _field_row_to_dim(row) if row else None


def resolve_joins_for_tables(needed_physical: set[str]) -> list[dict]:
    """Return enabled rels whose dim physical name is in needed set (vs primary fact)."""
    fact = get_primary_fact_table()
    if not fact:
        return []
    fact_phys = fact["physical_name"]
    needed = {t for t in needed_physical if t and t != fact_phys}
    if not needed:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*, d.physical_name AS dim_physical, f.physical_name AS fact_physical
               FROM meta_table_rel r
               JOIN meta_table d ON d.id = r.dim_table_id
               JOIN meta_table f ON f.id = r.fact_table_id
               WHERE r.enabled=1 AND f.id=?""",
            (fact["id"],),
        ).fetchall()
    out = []
    for r in rows:
        if r["dim_physical"] in needed:
            item = dict(r)
            item["join_keys_list"] = json.loads(r["join_keys"] or "[]")
            out.append(item)
    return out


def dim_alias_for(physical_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", physical_name)
    if not safe or safe[0].isdigit():
        safe = "d_" + safe
    return f"d_{safe}"[:40]

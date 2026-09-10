"""SQLite schema, connection helpers, and seed data."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "demo.db"

STAGES = [
    ("PJ", "pj_total_cost", "pj_exchange_rate"),
    ("Contract", "contract_total_cost", "contract_exchange_rate"),
    ("Target", "target_total_cost", "target_exchange_rate"),
    ("YTD", "ytd_total_cost", "ytd_exchange_rate"),
    ("FinalCST", "finalcst_total_cost", "finalcst_exchange_rate"),
]


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(force: bool = False) -> None:
    """Create tables and seed if empty (or force rebuild)."""
    if force and DB_PATH.exists():
        DB_PATH.unlink()

    with get_conn() as conn:
        _create_schema(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM metric_atomic").fetchone()["c"]
        if n == 0:
            _seed(conn)
        else:
            _ensure_dim_catalog_and_binds(conn)
        conn.commit()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metric_atomic (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            source_table TEXT NOT NULL,
            source_field TEXT NOT NULL,
            agg_type TEXT NOT NULL DEFAULT 'SUM',
            grain_dims TEXT NOT NULL DEFAULT '["project_number"]',
            remark TEXT
        );

        CREATE TABLE IF NOT EXISTS metric_derived (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            atomic_id TEXT NOT NULL,
            stage_type TEXT NOT NULL,
            amount_col TEXT NOT NULL,
            rate_col TEXT NOT NULL,
            grain_dims TEXT NOT NULL DEFAULT '["project_number"]',
            exposed INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (atomic_id) REFERENCES metric_atomic(id)
        );

        CREATE TABLE IF NOT EXISTS metric_composite (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            formula TEXT NOT NULL,
            sub_metric_ids TEXT NOT NULL,
            check_currency_same INTEGER NOT NULL DEFAULT 1,
            check_granularity_same INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS currency_rule (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            expr_template TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analysis_dim (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_field TEXT NOT NULL,
            dim_role TEXT NOT NULL DEFAULT 'attr',
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_no INTEGER NOT NULL DEFAULT 100,
            remark TEXT
        );

        CREATE TABLE IF NOT EXISTS metric_dim_bind (
            metric_type TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            dim_id TEXT NOT NULL,
            PRIMARY KEY (metric_type, metric_id, dim_id),
            FOREIGN KEY (dim_id) REFERENCES analysis_dim(id)
        );

        CREATE TABLE IF NOT EXISTS ads_fin_tracker_comparison_summary_df (
            project_number TEXT PRIMARY KEY,
            pj_total_cost REAL,
            pj_exchange_rate REAL,
            contract_total_cost REAL,
            contract_exchange_rate REAL,
            target_total_cost REAL,
            target_exchange_rate REAL,
            ytd_total_cost REAL,
            ytd_exchange_rate REAL,
            finalcst_total_cost REAL,
            finalcst_exchange_rate REAL
        );

        CREATE TABLE IF NOT EXISTS dim_project (
            project_number TEXT PRIMARY KEY,
            project_name TEXT,
            power_plant TEXT,
            region TEXT
        );
        """
    )


def _seed(conn: sqlite3.Connection) -> None:
    # Currency rules
    conn.executemany(
        "INSERT INTO currency_rule(code, name, expr_template) VALUES (?,?,?)",
        [
            ("CNY", "人民币", "COALESCE({amount},0) * COALESCE({rate},1)"),
            ("USD", "美元", "COALESCE({amount},0) / COALESCE({rate},1)"),
            ("ORIGIN", "原币", "COALESCE({amount},0)"),
        ],
    )

    _seed_analysis_dims(conn)

    # Atomic metrics: amounts + rates + dims
    atomics = []
    for stage, amount, rate in STAGES:
        atomics.append(
            (
                f"atom_{amount}",
                f"{stage}原始成本",
                "ads_fin_tracker_comparison_summary_df",
                amount,
                "SUM",
                '["project_number"]',
                f"{stage} 阶段金额原子",
            )
        )
        atomics.append(
            (
                f"atom_{rate}",
                f"{stage}汇率",
                "ads_fin_tracker_comparison_summary_df",
                rate,
                "MAX",
                '["project_number"]',
                f"{stage} 阶段汇率原子",
            )
        )
    atomics.extend(
        [
            (
                "atom_project_number",
                "项目编号",
                "dim_project",
                "project_number",
                "MAX",
                '["project_number"]',
                "维度原子",
            ),
            (
                "atom_power_plant",
                "电站",
                "dim_project",
                "power_plant",
                "MAX",
                '["project_number"]',
                "维度原子",
            ),
        ]
    )
    conn.executemany(
        """INSERT INTO metric_atomic
           (id, name, source_table, source_field, agg_type, grain_dims, remark)
           VALUES (?,?,?,?,?,?,?)""",
        atomics,
    )

    # Derived: five-stage total cost (exposed)
    derived = []
    for stage, amount, rate in STAGES:
        derived.append(
            (
                f"drv_{stage.lower()}_total_cost",
                f"{stage}总成本",
                f"atom_{amount}",
                stage,
                amount,
                rate,
                '["project_number"]',
                1,
            )
        )
    conn.executemany(
        """INSERT INTO metric_derived
           (id, name, atomic_id, stage_type, amount_col, rate_col, grain_dims, exposed)
           VALUES (?,?,?,?,?,?,?,?)""",
        derived,
    )

    # Composite: GAP and GAP rate
    conn.execute(
        """INSERT INTO metric_composite
           (id, name, formula, sub_metric_ids, check_currency_same, check_granularity_same)
           VALUES (?,?,?,?,?,?)""",
        (
            "cmp_cost_gap",
            "成本GAP",
            "Contract总成本 - PJ总成本",
            json.dumps(["drv_contract_total_cost", "drv_pj_total_cost"]),
            1,
            1,
        ),
    )
    conn.execute(
        """INSERT INTO metric_composite
           (id, name, formula, sub_metric_ids, check_currency_same, check_granularity_same)
           VALUES (?,?,?,?,?,?)""",
        (
            "cmp_cost_gap_rate",
            "成本GAP率",
            "成本GAP / Contract总成本",
            json.dumps(["cmp_cost_gap", "drv_contract_total_cost"]),
            1,
            1,
        ),
    )

    # Sample business data
    conn.executemany(
        """INSERT INTO dim_project(project_number, project_name, power_plant, region)
           VALUES (?,?,?,?)""",
        [
            ("P001", "华东支架项目A", "电站甲", "华东"),
            ("P002", "华北支架项目B", "电站乙", "华北"),
            ("P003", "华南支架项目C", "电站丙", "华南"),
        ],
    )
    # rates differ by stage on purpose (to prove no cross-stage mix)
    conn.executemany(
        """INSERT INTO ads_fin_tracker_comparison_summary_df(
            project_number,
            pj_total_cost, pj_exchange_rate,
            contract_total_cost, contract_exchange_rate,
            target_total_cost, target_exchange_rate,
            ytd_total_cost, ytd_exchange_rate,
            finalcst_total_cost, finalcst_exchange_rate
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("P001", 1000.0, 7.1, 1200.0, 7.2, 1100.0, 7.15, 800.0, 7.18, 1250.0, 7.25),
            ("P002", 2000.0, 7.0, 2100.0, 7.1, 2050.0, 7.05, 1500.0, 7.08, 2200.0, 7.12),
            ("P003", 1500.0, 6.9, 1600.0, 7.0, 1550.0, 6.95, 900.0, 6.98, 1650.0, 7.05),
        ],
    )

    _seed_default_metric_dim_binds(conn)


def _seed_analysis_dims(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """INSERT OR IGNORE INTO analysis_dim
           (id, code, name, source_table, source_field, dim_role, enabled, sort_no, remark)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (
                "dim_project_number",
                "project_number",
                "项目编号",
                "ads_fin_tracker_comparison_summary_df",
                "project_number",
                "grain",
                1,
                10,
                "项目粒度主键",
            ),
            (
                "dim_power_plant",
                "power_plant",
                "电站",
                "dim_project",
                "power_plant",
                "attr",
                1,
                20,
                "分析展示维度",
            ),
            (
                "dim_region",
                "region",
                "区域",
                "dim_project",
                "region",
                "attr",
                1,
                30,
                "分析展示维度",
            ),
            (
                "dim_project_name",
                "project_name",
                "项目名称",
                "dim_project",
                "project_name",
                "attr",
                1,
                40,
                "分析展示维度",
            ),
        ],
    )


def _seed_default_metric_dim_binds(conn: sqlite3.Connection) -> None:
    """Bind grain + common analysis dims to seeded metrics."""
    grain = "dim_project_number"
    attrs = ["dim_power_plant", "dim_region", "dim_project_name"]
    all_dims = [grain] + attrs

    for row in conn.execute("SELECT id FROM metric_atomic"):
        for did in all_dims:
            conn.execute(
                "INSERT OR IGNORE INTO metric_dim_bind(metric_type, metric_id, dim_id) VALUES (?,?,?)",
                ("atomic", row["id"], did),
            )
    for row in conn.execute("SELECT id FROM metric_derived"):
        for did in all_dims:
            conn.execute(
                "INSERT OR IGNORE INTO metric_dim_bind(metric_type, metric_id, dim_id) VALUES (?,?,?)",
                ("derived", row["id"], did),
            )
    for row in conn.execute("SELECT id FROM metric_composite"):
        for did in all_dims:
            conn.execute(
                "INSERT OR IGNORE INTO metric_dim_bind(metric_type, metric_id, dim_id) VALUES (?,?,?)",
                ("composite", row["id"], did),
            )


def _ensure_dim_catalog_and_binds(conn: sqlite3.Connection) -> None:
    """Migrate existing DB: ensure dim catalog + backfill binds from grain_dims."""
    _seed_analysis_dims(conn)
    n = conn.execute("SELECT COUNT(*) AS c FROM metric_dim_bind").fetchone()["c"]
    if n > 0:
        return

    code_to_id = {
        r["code"]: r["id"]
        for r in conn.execute("SELECT id, code FROM analysis_dim")
    }
    grain_id = code_to_id.get("project_number")
    attr_ids = [
        code_to_id[c]
        for c in ("power_plant", "region", "project_name")
        if c in code_to_id
    ]

    def bind(mtype: str, mid: str, dim_ids: list[str]) -> None:
        for did in dim_ids:
            conn.execute(
                "INSERT OR IGNORE INTO metric_dim_bind(metric_type, metric_id, dim_id) VALUES (?,?,?)",
                (mtype, mid, did),
            )

    for row in conn.execute("SELECT id, grain_dims FROM metric_atomic"):
        codes = json.loads(row["grain_dims"] or "[]")
        ids = [code_to_id[c] for c in codes if c in code_to_id]
        if grain_id and grain_id not in ids:
            ids.insert(0, grain_id)
        bind("atomic", row["id"], ids + attr_ids)

    for row in conn.execute("SELECT id, grain_dims FROM metric_derived"):
        codes = json.loads(row["grain_dims"] or "[]")
        ids = [code_to_id[c] for c in codes if c in code_to_id]
        if grain_id and grain_id not in ids:
            ids.insert(0, grain_id)
        bind("derived", row["id"], ids + attr_ids)

    for row in conn.execute("SELECT id FROM metric_composite"):
        ids = ([grain_id] if grain_id else []) + attr_ids
        bind("composite", row["id"], ids)


# ---------- CRUD helpers ----------

def list_atomic() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute("SELECT * FROM metric_atomic ORDER BY name"))


def get_atomic(metric_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM metric_atomic WHERE id=?", (metric_id,)
        ).fetchone()


def upsert_atomic(data: dict, dim_ids: list[str] | None = None) -> None:
    with get_conn() as conn:
        if dim_ids is not None:
            data = dict(data)
            data["grain_dims"] = json.dumps(_grain_codes_from_dim_ids(conn, dim_ids))
        conn.execute(
            """INSERT INTO metric_atomic
               (id, name, source_table, source_field, agg_type, grain_dims, remark)
               VALUES (:id,:name,:source_table,:source_field,:agg_type,:grain_dims,:remark)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 source_table=excluded.source_table,
                 source_field=excluded.source_field,
                 agg_type=excluded.agg_type,
                 grain_dims=excluded.grain_dims,
                 remark=excluded.remark""",
            data,
        )
        if dim_ids is not None:
            _replace_binds(conn, "atomic", data["id"], dim_ids)
        conn.commit()


def delete_atomic(metric_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM metric_dim_bind WHERE metric_type=? AND metric_id=?",
            ("atomic", metric_id),
        )
        conn.execute("DELETE FROM metric_atomic WHERE id=?", (metric_id,))
        conn.commit()


def list_derived(exposed_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM metric_derived"
    if exposed_only:
        sql += " WHERE exposed=1"
    sql += " ORDER BY stage_type, name"
    with get_conn() as conn:
        return list(conn.execute(sql))


def get_derived(metric_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM metric_derived WHERE id=?", (metric_id,)
        ).fetchone()


def get_derived_by_name(name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM metric_derived WHERE name=?", (name,)
        ).fetchone()


def upsert_derived(data: dict, dim_ids: list[str] | None = None) -> None:
    with get_conn() as conn:
        if dim_ids is not None:
            data = dict(data)
            data["grain_dims"] = json.dumps(_grain_codes_from_dim_ids(conn, dim_ids))
        conn.execute(
            """INSERT INTO metric_derived
               (id, name, atomic_id, stage_type, amount_col, rate_col, grain_dims, exposed)
               VALUES (:id,:name,:atomic_id,:stage_type,:amount_col,:rate_col,:grain_dims,:exposed)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 atomic_id=excluded.atomic_id,
                 stage_type=excluded.stage_type,
                 amount_col=excluded.amount_col,
                 rate_col=excluded.rate_col,
                 grain_dims=excluded.grain_dims,
                 exposed=excluded.exposed""",
            data,
        )
        if dim_ids is not None:
            _replace_binds(conn, "derived", data["id"], dim_ids)
        conn.commit()


def delete_derived(metric_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM metric_dim_bind WHERE metric_type=? AND metric_id=?",
            ("derived", metric_id),
        )
        conn.execute("DELETE FROM metric_derived WHERE id=?", (metric_id,))
        conn.commit()


def list_composite() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute("SELECT * FROM metric_composite ORDER BY name"))


def get_composite(metric_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM metric_composite WHERE id=?", (metric_id,)
        ).fetchone()


def get_composite_by_name(name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM metric_composite WHERE name=?", (name,)
        ).fetchone()


def upsert_composite(data: dict, dim_ids: list[str] | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO metric_composite
               (id, name, formula, sub_metric_ids, check_currency_same, check_granularity_same)
               VALUES (:id,:name,:formula,:sub_metric_ids,:check_currency_same,:check_granularity_same)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 formula=excluded.formula,
                 sub_metric_ids=excluded.sub_metric_ids,
                 check_currency_same=excluded.check_currency_same,
                 check_granularity_same=excluded.check_granularity_same""",
            data,
        )
        if dim_ids is not None:
            _replace_binds(conn, "composite", data["id"], dim_ids)
        conn.commit()


def delete_composite(metric_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM metric_dim_bind WHERE metric_type=? AND metric_id=?",
            ("composite", metric_id),
        )
        conn.execute("DELETE FROM metric_composite WHERE id=?", (metric_id,))
        conn.commit()


def list_currency_rules() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute("SELECT * FROM currency_rule ORDER BY code"))


def get_currency_rule(code: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM currency_rule WHERE code=?", (code,)
        ).fetchone()


def stage_map() -> dict[str, tuple[str, str]]:
    return {s: (a, r) for s, a, r in STAGES}


# ---------- Analysis dimension catalog & binds ----------

def list_analysis_dims(enabled_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM analysis_dim"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort_no, code"
    with get_conn() as conn:
        return list(conn.execute(sql))


def get_analysis_dim(dim_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM analysis_dim WHERE id=?", (dim_id,)
        ).fetchone()


def get_analysis_dim_by_code(code: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM analysis_dim WHERE code=?", (code,)
        ).fetchone()


def upsert_analysis_dim(data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO analysis_dim
               (id, code, name, source_table, source_field, dim_role, enabled, sort_no, remark)
               VALUES (:id,:code,:name,:source_table,:source_field,:dim_role,:enabled,:sort_no,:remark)
               ON CONFLICT(id) DO UPDATE SET
                 code=excluded.code,
                 name=excluded.name,
                 source_table=excluded.source_table,
                 source_field=excluded.source_field,
                 dim_role=excluded.dim_role,
                 enabled=excluded.enabled,
                 sort_no=excluded.sort_no,
                 remark=excluded.remark""",
            data,
        )
        conn.commit()


def delete_analysis_dim(dim_id: str) -> None:
    with get_conn() as conn:
        used = conn.execute(
            "SELECT COUNT(*) AS c FROM metric_dim_bind WHERE dim_id=?", (dim_id,)
        ).fetchone()["c"]
        if used:
            raise ValueError(f"维度仍被 {used} 个指标绑定，无法删除")
        conn.execute("DELETE FROM analysis_dim WHERE id=?", (dim_id,))
        conn.commit()


def list_metric_dim_binds(metric_type: str, metric_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(
            conn.execute(
                """SELECT d.*
                   FROM metric_dim_bind b
                   JOIN analysis_dim d ON d.id = b.dim_id
                   WHERE b.metric_type=? AND b.metric_id=?
                   ORDER BY d.sort_no, d.code""",
                (metric_type, metric_id),
            )
        )


def list_metric_dim_ids(metric_type: str, metric_id: str) -> list[str]:
    return [r["id"] for r in list_metric_dim_binds(metric_type, metric_id)]


def metric_bound_dim_labels(metric_type: str, metric_id: str) -> str:
    rows = list_metric_dim_binds(metric_type, metric_id)
    if not rows:
        return "-"
    parts = []
    for r in rows:
        tag = "粒" if r["dim_role"] == "grain" else "析"
        parts.append(f"{r['name']}[{tag}]")
    return "、".join(parts)


def resolve_metric_type(metric_id: str) -> str | None:
    if get_derived(metric_id) or get_derived_by_name(metric_id):
        return "derived"
    if get_composite(metric_id) or get_composite_by_name(metric_id):
        return "composite"
    if get_atomic(metric_id):
        return "atomic"
    return None


def get_metric_grain_codes(metric_type: str, metric_id: str) -> list[str]:
    rows = list_metric_dim_binds(metric_type, metric_id)
    grains = [r["code"] for r in rows if r["dim_role"] == "grain"]
    if grains:
        return grains
    # fallback legacy JSON
    if metric_type == "atomic":
        row = get_atomic(metric_id)
        return json.loads(row["grain_dims"]) if row else ["project_number"]
    if metric_type == "derived":
        row = get_derived(metric_id)
        return json.loads(row["grain_dims"]) if row else ["project_number"]
    return ["project_number"]


def get_metric_analysis_codes(metric_type: str, metric_id: str) -> list[str]:
    rows = list_metric_dim_binds(metric_type, metric_id)
    return [r["code"] for r in rows if r["dim_role"] == "attr"]


def _replace_binds(
    conn: sqlite3.Connection, metric_type: str, metric_id: str, dim_ids: list[str]
) -> None:
    if not dim_ids:
        raise ValueError("至少绑定一个分析/粒度维度")
    # must include at least one grain for atomic/derived; composite recommended
    roles = {
        r["id"]: r["dim_role"]
        for r in conn.execute(
            f"SELECT id, dim_role FROM analysis_dim WHERE id IN ({','.join('?'*len(dim_ids))})",
            dim_ids,
        )
    }
    if len(roles) != len(set(dim_ids)):
        raise ValueError("存在无效维度 ID")
    if metric_type in ("atomic", "derived") and not any(
        roles[i] == "grain" for i in dim_ids
    ):
        raise ValueError("原子/派生指标必须至少绑定一个「粒度」维度")
    conn.execute(
        "DELETE FROM metric_dim_bind WHERE metric_type=? AND metric_id=?",
        (metric_type, metric_id),
    )
    for did in dim_ids:
        conn.execute(
            "INSERT INTO metric_dim_bind(metric_type, metric_id, dim_id) VALUES (?,?,?)",
            (metric_type, metric_id, did),
        )


def _grain_codes_from_dim_ids(conn: sqlite3.Connection, dim_ids: list[str]) -> list[str]:
    if not dim_ids:
        return ["project_number"]
    rows = conn.execute(
        f"""SELECT code FROM analysis_dim
            WHERE id IN ({','.join('?'*len(dim_ids))}) AND dim_role='grain'
            ORDER BY sort_no""",
        dim_ids,
    ).fetchall()
    codes = [r["code"] for r in rows]
    return codes or ["project_number"]

"""SQLite schema, connection helpers, and seed data."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import metric_sql
import meta
import biz_arch

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "demo.db"

STAGES = [
    ("PJ", "pj_total_cost", "pj_exchange_rate"),
    ("Contract", "contract_total_cost", "contract_exchange_rate"),
    ("Target", "target_total_cost", "target_exchange_rate"),
    ("YTD", "ytd_total_cost", "ytd_exchange_rate"),
    ("FinalCST", "finalcst_total_cost", "finalcst_exchange_rate"),
]


def parse_aliases(raw: str | None) -> list[str]:
    """Split aliases text (comma / Chinese comma / semicolon / newline) into unique labels."""
    text = (raw or "").strip()
    if not text:
        return []
    for sep in ("\n", "；", ";", "，"):
        text = text.replace(sep, ",")
    seen: set[str] = set()
    out: list[str] = []
    for part in text.split(","):
        s = part.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def format_aliases(values: list[str] | None) -> str:
    """Normalize alias list to comma-separated storage text."""
    return ", ".join(parse_aliases(",".join(values or [])))


def normalize_aliases_input(raw: str | None) -> str:
    return format_aliases(parse_aliases(raw))


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
        meta.ensure_schema(conn)
        biz_arch.ensure_schema(conn)
        _migrate_schema(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM metric_atomic").fetchone()["c"]
        if n == 0:
            _seed(conn)
            meta.seed_demo_meta(conn)
            biz_arch.seed_demo(conn)
            _seed_default_metric_dim_binds(conn)
        else:
            _backfill_atomic_fx_from_derived(conn)
            meta.seed_demo_meta(conn)
            biz_arch.seed_demo(conn)
            _ensure_metric_dim_bind_schema(conn)
            _migrate_binds_to_meta_fields(conn)
            _seed_default_metric_dim_binds(conn)
        conn.commit()


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = _table_cols(conn, table)
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


TAXONOMY_KEYS = ("biz_line", "theme_domain", "biz_object", "biz_process")


def empty_taxonomy() -> dict[str, str]:
    return {k: "" for k in TAXONOMY_KEYS}


def _row_taxonomy(row: sqlite3.Row | None) -> dict[str, str]:
    if not row:
        return empty_taxonomy()
    keys = set(row.keys())
    return {k: (row[k] or "") if k in keys else "" for k in TAXONOMY_KEYS}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add filter / SQL / FX / taxonomy columns to existing DBs."""
    _add_col(conn, "metric_atomic", "filter_json", "TEXT NOT NULL DEFAULT '[]'")
    _add_col(conn, "metric_atomic", "sql_expr", "TEXT")
    _add_col(conn, "metric_atomic", "sql_manual", "INTEGER NOT NULL DEFAULT 0")
    _add_col(conn, "metric_atomic", "stage_type", "TEXT")
    _add_col(conn, "metric_atomic", "rate_col", "TEXT")
    _add_col(conn, "metric_atomic", "name_en", "TEXT")
    _add_col(conn, "metric_atomic", "aliases", "TEXT NOT NULL DEFAULT ''")
    for col in TAXONOMY_KEYS:
        _add_col(conn, "metric_atomic", col, "TEXT")
    _add_col(conn, "metric_derived", "filter_json", "TEXT NOT NULL DEFAULT '[]'")
    _add_col(conn, "metric_derived", "aliases", "TEXT NOT NULL DEFAULT ''")
    for col in TAXONOMY_KEYS:
        _add_col(conn, "metric_derived", col, "TEXT")
    _add_col(conn, "metric_composite", "aliases", "TEXT NOT NULL DEFAULT ''")
    for col in TAXONOMY_KEYS:
        _add_col(conn, "metric_composite", col, "TEXT")
    # Demo alias backfill (only when empty; safe for existing DBs)
    for table, pairs in (
        (
            "metric_atomic",
            [
                ("atom_power_plant", "电站名称, 电厂"),
                ("atom_pj_total_cost", "PJ成本, PJ金额"),
                ("atom_contract_total_cost", "合同成本, Contract金额"),
            ],
        ),
        (
            "metric_derived",
            [
                ("drv_pj_total_cost", "PJ总成本额, PJ成本合计"),
                ("drv_contract_total_cost", "合同总成本, Contract成本"),
            ],
        ),
        (
            "metric_composite",
            [
                ("cmp_cost_gap", "成本差距, Cost GAP"),
                ("cmp_cost_gap_rate", "GAP率, 成本差距率"),
            ],
        ),
    ):
        for mid, aliases in pairs:
            conn.execute(
                f"""UPDATE {table}
                   SET aliases=?
                   WHERE id=? AND (aliases IS NULL OR TRIM(aliases)='')""",
                (aliases, mid),
            )
    _migrate_metric_dim_bind_drop_fk(conn)
    # backfill derived taxonomy from atomic when empty
    conn.execute(
        """UPDATE metric_derived
           SET biz_line = (SELECT a.biz_line FROM metric_atomic a WHERE a.id = metric_derived.atomic_id),
               theme_domain = (SELECT a.theme_domain FROM metric_atomic a WHERE a.id = metric_derived.atomic_id),
               biz_object = (SELECT a.biz_object FROM metric_atomic a WHERE a.id = metric_derived.atomic_id),
               biz_process = (SELECT a.biz_process FROM metric_atomic a WHERE a.id = metric_derived.atomic_id)
           WHERE COALESCE(TRIM(biz_line), '') = ''
             AND EXISTS (SELECT 1 FROM metric_atomic a WHERE a.id = metric_derived.atomic_id)"""
    )
    # backfill English names from source_field when empty
    conn.execute(
        """UPDATE metric_atomic
           SET name_en = source_field
           WHERE name_en IS NULL OR TRIM(name_en) = ''"""
    )
    # refresh auto SQL to new default format when not manually overridden
    for atom in conn.execute(
        """SELECT id, source_table, source_field, name_en, filter_json, sql_manual
           FROM metric_atomic
           WHERE COALESCE(sql_manual, 0) = 0"""
    ):
        try:
            preview = metric_sql.build_atomic_sql_preview(
                source_table=atom["source_table"],
                source_field=atom["source_field"],
                name_en=atom["name_en"] or atom["source_field"],
                filter_text=metric_sql.normalize_filter_text(atom["filter_json"] or ""),
            )
            conn.execute(
                "UPDATE metric_atomic SET sql_expr=? WHERE id=?",
                (preview, atom["id"]),
            )
        except ValueError:
            continue


def _migrate_metric_dim_bind_drop_fk(conn: sqlite3.Connection) -> None:
    """Drop FK to analysis_dim so dim_id can reference meta_field.id."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='metric_dim_bind'"
    ).fetchone()
    if not row or "analysis_dim" not in (row["sql"] or ""):
        return
    conn.executescript(
        """
        CREATE TABLE metric_dim_bind_new (
            metric_type TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            dim_id TEXT NOT NULL,
            PRIMARY KEY (metric_type, metric_id, dim_id)
        );
        INSERT OR IGNORE INTO metric_dim_bind_new
            SELECT metric_type, metric_id, dim_id FROM metric_dim_bind;
        DROP TABLE metric_dim_bind;
        ALTER TABLE metric_dim_bind_new RENAME TO metric_dim_bind;
        """
    )

def _backfill_atomic_fx_from_derived(conn: sqlite3.Connection) -> None:
    """Move stage FX pair from derived onto linked amount atomics when empty."""
    for row in conn.execute(
        """SELECT d.atomic_id, d.stage_type, d.amount_col, d.rate_col
           FROM metric_derived d"""
    ):
        atom = conn.execute(
            "SELECT id, stage_type, rate_col, source_field, name_en, filter_json, sql_expr, sql_manual, source_table, agg_type FROM metric_atomic WHERE id=?",
            (row["atomic_id"],),
        ).fetchone()
        if not atom:
            continue
        stage = atom["stage_type"] or row["stage_type"]
        rate = atom["rate_col"] or row["rate_col"]
        conn.execute(
            "UPDATE metric_atomic SET stage_type=?, rate_col=? WHERE id=?",
            (stage, rate, atom["id"]),
        )
        if not atom["sql_expr"]:
            filter_text = metric_sql.normalize_filter_text(atom["filter_json"] or "")
            name_en = atom["name_en"] or atom["source_field"]
            preview = metric_sql.build_atomic_sql_preview(
                source_table=atom["source_table"],
                source_field=atom["source_field"],
                name_en=name_en,
                filter_text=filter_text,
            )
            conn.execute(
                "UPDATE metric_atomic SET sql_expr=?, name_en=COALESCE(NULLIF(name_en,''), ?) WHERE id=?",
                (preview, name_en, atom["id"]),
            )


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metric_atomic (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            name_en TEXT,
            source_table TEXT NOT NULL,
            source_field TEXT NOT NULL,
            agg_type TEXT NOT NULL DEFAULT 'SUM',
            grain_dims TEXT NOT NULL DEFAULT '["project_number"]',
            remark TEXT,
            filter_json TEXT NOT NULL DEFAULT '',
            sql_expr TEXT,
            sql_manual INTEGER NOT NULL DEFAULT 0,
            stage_type TEXT,
            rate_col TEXT,
            aliases TEXT NOT NULL DEFAULT '',
            biz_line TEXT,
            theme_domain TEXT,
            biz_object TEXT,
            biz_process TEXT
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
            filter_json TEXT NOT NULL DEFAULT '',
            aliases TEXT NOT NULL DEFAULT '',
            biz_line TEXT,
            theme_domain TEXT,
            biz_object TEXT,
            biz_process TEXT,
            FOREIGN KEY (atomic_id) REFERENCES metric_atomic(id)
        );

        CREATE TABLE IF NOT EXISTS metric_composite (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            formula TEXT NOT NULL,
            sub_metric_ids TEXT NOT NULL,
            check_currency_same INTEGER NOT NULL DEFAULT 1,
            check_granularity_same INTEGER NOT NULL DEFAULT 1,
            aliases TEXT NOT NULL DEFAULT '',
            biz_line TEXT,
            theme_domain TEXT,
            biz_object TEXT,
            biz_process TEXT
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
            PRIMARY KEY (metric_type, metric_id, dim_id)
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

    # Atomic metrics: amounts (+ stage FX) + rates + dims
    # taxonomy: 业务线=支架 / 主题域=财务 / 业务对象=项目 / 业务过程=阶段
    dim_tax = ("支架", "财务", "项目", "")
    atomics = []
    for stage, amount, rate in STAGES:
        fin_tax = ("支架", "财务", "项目", stage)
        amount_sql = metric_sql.build_atomic_sql_preview(
            source_table="ads_fin_tracker_comparison_summary_df",
            source_field=amount,
            name_en=amount,
            filter_text="",
        )
        atomics.append(
            (
                f"atom_{amount}",
                f"{stage}原始成本",
                amount,
                "ads_fin_tracker_comparison_summary_df",
                amount,
                "SUM",
                '["project_number"]',
                f"{stage} 阶段金额原子（汇率字段在本层维护）",
                "",
                amount_sql,
                0,
                stage,
                rate,
                *fin_tax,
            )
        )
        rate_sql = metric_sql.build_atomic_sql_preview(
            source_table="ads_fin_tracker_comparison_summary_df",
            source_field=rate,
            name_en=rate,
            filter_text="",
        )
        atomics.append(
            (
                f"atom_{rate}",
                f"{stage}汇率",
                rate,
                "ads_fin_tracker_comparison_summary_df",
                rate,
                "MAX",
                '["project_number"]',
                f"{stage} 阶段汇率原子",
                "",
                rate_sql,
                0,
                stage,
                "",
                *fin_tax,
            )
        )
    for aid, name, name_en, table, field, agg, remark in [
        (
            "atom_project_number",
            "项目编号",
            "project_number",
            "dim_project",
            "project_number",
            "MAX",
            "维度原子",
        ),
        (
            "atom_power_plant",
            "电站",
            "power_plant",
            "dim_project",
            "power_plant",
            "MAX",
            "维度原子",
        ),
    ]:
        preview = metric_sql.build_atomic_sql_preview(
            source_table=table, source_field=field, name_en=name_en, filter_text=""
        )
        atomics.append(
            (
                aid,
                name,
                name_en,
                table,
                field,
                agg,
                '["project_number"]',
                remark,
                "",
                preview,
                0,
                "",
                "",
                *dim_tax,
            )
        )
    conn.executemany(
        """INSERT INTO metric_atomic
           (id, name, name_en, source_table, source_field, agg_type, grain_dims, remark,
            filter_json, sql_expr, sql_manual, stage_type, rate_col,
            biz_line, theme_domain, biz_object, biz_process)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        atomics,
    )

    # Derived: five-stage total cost (FX + taxonomy inherited from atomic)
    derived = []
    for stage, amount, rate in STAGES:
        fin_tax = ("支架", "财务", "项目", stage)
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
                "",
                *fin_tax,
            )
        )
    conn.executemany(
        """INSERT INTO metric_derived
           (id, name, atomic_id, stage_type, amount_col, rate_col, grain_dims, exposed, filter_json,
            biz_line, theme_domain, biz_object, biz_process)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        derived,
    )

    # Composite: GAP and GAP rate (default from first formula metric · Contract)
    gap_tax = ("支架", "财务", "项目", "Contract")
    conn.execute(
        """INSERT INTO metric_composite
           (id, name, formula, sub_metric_ids, check_currency_same, check_granularity_same,
            biz_line, theme_domain, biz_object, biz_process)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "cmp_cost_gap",
            "成本GAP",
            "Contract总成本 - PJ总成本",
            json.dumps(["drv_contract_total_cost", "drv_pj_total_cost"]),
            1,
            1,
            *gap_tax,
        ),
    )
    conn.execute(
        """INSERT INTO metric_composite
           (id, name, formula, sub_metric_ids, check_currency_same, check_granularity_same,
            biz_line, theme_domain, biz_object, biz_process)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "cmp_cost_gap_rate",
            "成本GAP率",
            "成本GAP / Contract总成本",
            json.dumps(["cmp_cost_gap", "drv_contract_total_cost"]),
            1,
            1,
            *gap_tax,
        ),
    )

    # Demo aliases for list「别名」展示（问数同义词可后续接入）
    conn.executemany(
        "UPDATE metric_atomic SET aliases=? WHERE id=?",
        [
            ("电站名称, 电厂", "atom_power_plant"),
            ("PJ成本, PJ金额", "atom_pj_total_cost"),
            ("合同成本, Contract金额", "atom_contract_total_cost"),
        ],
    )
    conn.executemany(
        "UPDATE metric_derived SET aliases=? WHERE id=?",
        [
            ("PJ总成本额, PJ成本合计", "drv_pj_total_cost"),
            ("合同总成本, Contract成本", "drv_contract_total_cost"),
        ],
    )
    conn.executemany(
        "UPDATE metric_composite SET aliases=? WHERE id=?",
        [
            ("成本差距, Cost GAP", "cmp_cost_gap"),
            ("GAP率, 成本差距率", "cmp_cost_gap_rate"),
        ],
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


def _seed_default_metric_dim_binds(conn: sqlite3.Connection) -> None:
    """Bind grain + analysis meta fields to metrics that have no binds yet."""
    fields = list(
        conn.execute(
            """SELECT f.id, f.semantic_role
               FROM meta_field f
               JOIN meta_table t ON t.id = f.table_id
               WHERE f.is_analysis_dim=1 AND f.enabled=1 AND t.enabled=1
                 AND f.semantic_role IN ('grain','attr')
               ORDER BY CASE f.semantic_role WHEN 'grain' THEN 0 ELSE 1 END,
                        f.sort_no"""
        )
    )
    if not fields:
        return
    field_ids = [f["id"] for f in fields]

    def ensure_binds(mtype: str, mid: str) -> None:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM metric_dim_bind WHERE metric_type=? AND metric_id=?",
            (mtype, mid),
        ).fetchone()["c"]
        if n:
            return
        for fid in field_ids:
            conn.execute(
                "INSERT OR IGNORE INTO metric_dim_bind(metric_type, metric_id, dim_id) VALUES (?,?,?)",
                (mtype, mid, fid),
            )

    for row in conn.execute("SELECT id FROM metric_atomic"):
        ensure_binds("atomic", row["id"])
    for row in conn.execute("SELECT id FROM metric_derived"):
        ensure_binds("derived", row["id"])
    for row in conn.execute("SELECT id FROM metric_composite"):
        ensure_binds("composite", row["id"])


def _ensure_metric_dim_bind_schema(conn: sqlite3.Connection) -> None:
    """Drop legacy FK to analysis_dim so dim_id can reference meta_field."""
    fks = list(conn.execute("PRAGMA foreign_key_list(metric_dim_bind)"))
    if not any(fk[2] == "analysis_dim" for fk in fks):
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metric_dim_bind__new (
            metric_type TEXT NOT NULL,
            metric_id TEXT NOT NULL,
            dim_id TEXT NOT NULL,
            PRIMARY KEY (metric_type, metric_id, dim_id)
        );
        INSERT OR IGNORE INTO metric_dim_bind__new
            SELECT metric_type, metric_id, dim_id FROM metric_dim_bind;
        DROP TABLE metric_dim_bind;
        ALTER TABLE metric_dim_bind__new RENAME TO metric_dim_bind;
        """
    )


def _migrate_binds_to_meta_fields(conn: sqlite3.Connection) -> None:
    """Remap metric_dim_bind.dim_id from analysis_dim ids to meta_field ids."""
    sample = conn.execute("SELECT dim_id FROM metric_dim_bind LIMIT 1").fetchone()
    if not sample:
        return
    # already pointing at meta_field
    if conn.execute(
        "SELECT 1 FROM meta_field WHERE id=?", (sample["dim_id"],)
    ).fetchone():
        return
    # build analysis_dim.code -> meta_field.id (prefer dim table for attr)
    code_to_field: dict[str, str] = {}
    for r in conn.execute(
        """SELECT f.id, f.field_name, f.semantic_role, t.table_kind
           FROM meta_field f
           JOIN meta_table t ON t.id = f.table_id
           WHERE f.is_analysis_dim=1
           ORDER BY CASE
             WHEN f.semantic_role='grain' AND t.table_kind='fact' THEN 0
             WHEN t.table_kind='dim' THEN 1
             ELSE 2 END"""
    ):
        code_to_field.setdefault(r["field_name"], r["id"])

    if not code_to_field:
        return

    adim_to_code = {
        r["id"]: r["code"]
        for r in conn.execute("SELECT id, code FROM analysis_dim")
    }

    rows = list(
        conn.execute("SELECT metric_type, metric_id, dim_id FROM metric_dim_bind")
    )
    conn.execute("DELETE FROM metric_dim_bind")
    for r in rows:
        code = adim_to_code.get(r["dim_id"])
        fid = code_to_field.get(code) if code else None
        if not fid:
            fid = code_to_field.get(r["dim_id"])
        if fid:
            conn.execute(
                "INSERT OR IGNORE INTO metric_dim_bind(metric_type, metric_id, dim_id) VALUES (?,?,?)",
                (r["metric_type"], r["metric_id"], fid),
            )


# ---------- CRUD helpers ----------

def list_atomic() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(conn.execute("SELECT * FROM metric_atomic ORDER BY name"))


def get_atomic(metric_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM metric_atomic WHERE id=?", (metric_id,)
        ).fetchone()


def get_atomic_by_name(name: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM metric_atomic WHERE name=?", (name,)
        ).fetchone()


def upsert_atomic(data: dict, dim_ids: list[str] | None = None) -> None:
    with get_conn() as conn:
        data = dict(data)
        if dim_ids is not None:
            data["grain_dims"] = json.dumps(_grain_codes_from_dim_ids(conn, dim_ids))
        data.setdefault("filter_json", "")
        data.setdefault("sql_manual", 0)
        data.setdefault("stage_type", "")
        data.setdefault("rate_col", "")
        data.setdefault("sql_expr", "")
        data.setdefault("name_en", data.get("source_field") or "")
        data["aliases"] = normalize_aliases_input(data.get("aliases"))
        for k in TAXONOMY_KEYS:
            data[k] = (data.get(k) or "").strip()
        # keep derived stage/amount/rate + taxonomy in sync when atomic changes
        conn.execute(
            """INSERT INTO metric_atomic
               (id, name, name_en, source_table, source_field, agg_type, grain_dims, remark,
                filter_json, sql_expr, sql_manual, stage_type, rate_col, aliases,
                biz_line, theme_domain, biz_object, biz_process)
               VALUES (:id,:name,:name_en,:source_table,:source_field,:agg_type,:grain_dims,:remark,
                       :filter_json,:sql_expr,:sql_manual,:stage_type,:rate_col,:aliases,
                       :biz_line,:theme_domain,:biz_object,:biz_process)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 name_en=excluded.name_en,
                 source_table=excluded.source_table,
                 source_field=excluded.source_field,
                 agg_type=excluded.agg_type,
                 grain_dims=excluded.grain_dims,
                 remark=excluded.remark,
                 filter_json=excluded.filter_json,
                 sql_expr=excluded.sql_expr,
                 sql_manual=excluded.sql_manual,
                 stage_type=excluded.stage_type,
                 rate_col=excluded.rate_col,
                 aliases=excluded.aliases,
                 biz_line=excluded.biz_line,
                 theme_domain=excluded.theme_domain,
                 biz_object=excluded.biz_object,
                 biz_process=excluded.biz_process""",
            data,
        )
        if data.get("stage_type") or data.get("rate_col"):
            conn.execute(
                """UPDATE metric_derived
                   SET stage_type=COALESCE(NULLIF(?, ''), stage_type),
                       amount_col=?,
                       rate_col=COALESCE(NULLIF(?, ''), rate_col)
                   WHERE atomic_id=?""",
                (
                    data.get("stage_type") or "",
                    data["source_field"],
                    data.get("rate_col") or "",
                    data["id"],
                ),
            )
        conn.execute(
            """UPDATE metric_derived
               SET biz_line=?, theme_domain=?, biz_object=?, biz_process=?
               WHERE atomic_id=?""",
            (
                data["biz_line"],
                data["theme_domain"],
                data["biz_object"],
                data["biz_process"],
                data["id"],
            ),
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
        data = dict(data)
        if dim_ids is not None:
            data["grain_dims"] = json.dumps(_grain_codes_from_dim_ids(conn, dim_ids))
        data.setdefault("filter_json", "")
        # FX + taxonomy always inherited from atomic layer
        atom = conn.execute(
            """SELECT source_field, stage_type, rate_col,
                      biz_line, theme_domain, biz_object, biz_process
               FROM metric_atomic WHERE id=?""",
            (data["atomic_id"],),
        ).fetchone()
        if not atom:
            raise ValueError(f"来源原子不存在: {data['atomic_id']}")
        data["amount_col"] = atom["source_field"]
        data["stage_type"] = atom["stage_type"] or data.get("stage_type") or ""
        data["rate_col"] = atom["rate_col"] or data.get("rate_col") or ""
        for k in TAXONOMY_KEYS:
            data[k] = (atom[k] or "").strip()
        if not data["stage_type"]:
            raise ValueError("来源原子未维护业务阶段 stage_type")
        if not data["rate_col"]:
            raise ValueError("来源原子未维护同阶段汇率字段 rate_col")
        data["aliases"] = normalize_aliases_input(data.get("aliases"))
        conn.execute(
            """INSERT INTO metric_derived
               (id, name, atomic_id, stage_type, amount_col, rate_col, grain_dims, exposed, filter_json,
                aliases, biz_line, theme_domain, biz_object, biz_process)
               VALUES (:id,:name,:atomic_id,:stage_type,:amount_col,:rate_col,:grain_dims,:exposed,:filter_json,
                       :aliases,:biz_line,:theme_domain,:biz_object,:biz_process)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 atomic_id=excluded.atomic_id,
                 stage_type=excluded.stage_type,
                 amount_col=excluded.amount_col,
                 rate_col=excluded.rate_col,
                 grain_dims=excluded.grain_dims,
                 exposed=excluded.exposed,
                 filter_json=excluded.filter_json,
                 aliases=excluded.aliases,
                 biz_line=excluded.biz_line,
                 theme_domain=excluded.theme_domain,
                 biz_object=excluded.biz_object,
                 biz_process=excluded.biz_process""",
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


def _metric_taxonomy_on_conn(conn: sqlite3.Connection, metric_id: str) -> dict[str, str]:
    """Resolve business taxonomy for any metric id (atomic / derived / composite)."""
    if not metric_id:
        return empty_taxonomy()
    row = conn.execute(
        """SELECT biz_line, theme_domain, biz_object, biz_process
           FROM metric_atomic WHERE id=?""",
        (metric_id,),
    ).fetchone()
    if row:
        return _row_taxonomy(row)
    row = conn.execute(
        """SELECT biz_line, theme_domain, biz_object, biz_process
           FROM metric_derived WHERE id=?""",
        (metric_id,),
    ).fetchone()
    if row:
        return _row_taxonomy(row)
    row = conn.execute(
        """SELECT biz_line, theme_domain, biz_object, biz_process
           FROM metric_composite WHERE id=?""",
        (metric_id,),
    ).fetchone()
    return _row_taxonomy(row)


def get_metric_taxonomy(metric_id: str) -> dict[str, str]:
    with get_conn() as conn:
        return _metric_taxonomy_on_conn(conn, metric_id)


def _metric_name_map(conn: sqlite3.Connection, metric_ids: list[str]) -> dict[str, str]:
    """id -> Chinese name for given metric ids across three layers."""
    if not metric_ids:
        return {}
    out: dict[str, str] = {}
    placeholders = ",".join("?" * len(metric_ids))
    for table in ("metric_atomic", "metric_derived", "metric_composite"):
        for r in conn.execute(
            f"SELECT id, name FROM {table} WHERE id IN ({placeholders})",
            metric_ids,
        ):
            out[r["id"]] = r["name"]
    return out


def first_formula_metric_id(
    formula: str, sub_metric_ids: list[str] | None = None
) -> str | None:
    """First metric appearing in formula text (longest-name scan); fallback to first sub id."""
    with get_conn() as conn:
        return _first_formula_metric_id_on_conn(conn, formula, sub_metric_ids or [])


def _first_formula_metric_id_on_conn(
    conn: sqlite3.Connection, formula: str, sub_metric_ids: list[str]
) -> str | None:
    ids = [x for x in (sub_metric_ids or []) if x]
    name_by_id = _metric_name_map(conn, ids)
    # If formula empty, use first checked sub
    formula = (formula or "").strip()
    if not formula:
        return ids[0] if ids else None
    # Prefer names of selected subs; if none, scan all known metrics
    if not name_by_id:
        name_by_id = {}
        for table in ("metric_atomic", "metric_derived", "metric_composite"):
            for r in conn.execute(f"SELECT id, name FROM {table}"):
                name_by_id[r["id"]] = r["name"]
    # Find earliest occurrence among longest names first (avoid partial overlaps)
    best_id = None
    best_pos = len(formula) + 1
    for mid, name in sorted(name_by_id.items(), key=lambda x: -len(x[1] or "")):
        if not name:
            continue
        pos = formula.find(name)
        if pos >= 0 and pos < best_pos:
            best_pos = pos
            best_id = mid
    if best_id:
        return best_id
    return ids[0] if ids else None


def taxonomy_from_formula(
    formula: str, sub_metric_ids: list[str] | None = None
) -> dict[str, str]:
    """Business taxonomy defaulted from the first metric in the formula."""
    with get_conn() as conn:
        mid = _first_formula_metric_id_on_conn(conn, formula, sub_metric_ids or [])
        return _metric_taxonomy_on_conn(conn, mid or "")


def upsert_composite(data: dict, dim_ids: list[str] | None = None) -> None:
    with get_conn() as conn:
        data = dict(data)
        subs = data.get("sub_metric_ids") or "[]"
        if isinstance(subs, str):
            try:
                sub_ids = json.loads(subs)
            except json.JSONDecodeError:
                sub_ids = []
        else:
            sub_ids = list(subs)
            data["sub_metric_ids"] = json.dumps(sub_ids)
        defaults = _metric_taxonomy_on_conn(
            conn,
            _first_formula_metric_id_on_conn(conn, data.get("formula") or "", sub_ids)
            or "",
        )
        # 业务线 / 主题域 always follow first formula metric
        data["biz_line"] = defaults["biz_line"]
        data["theme_domain"] = defaults["theme_domain"]
        # 业务对象 / 业务过程 editable; empty → default from first metric
        data["biz_object"] = (data.get("biz_object") or "").strip() or defaults[
            "biz_object"
        ]
        data["biz_process"] = (data.get("biz_process") or "").strip() or defaults[
            "biz_process"
        ]
        data["aliases"] = normalize_aliases_input(data.get("aliases"))
        conn.execute(
            """INSERT INTO metric_composite
               (id, name, formula, sub_metric_ids, check_currency_same, check_granularity_same,
                aliases, biz_line, theme_domain, biz_object, biz_process)
               VALUES (:id,:name,:formula,:sub_metric_ids,:check_currency_same,:check_granularity_same,
                       :aliases,:biz_line,:theme_domain,:biz_object,:biz_process)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 formula=excluded.formula,
                 sub_metric_ids=excluded.sub_metric_ids,
                 check_currency_same=excluded.check_currency_same,
                 check_granularity_same=excluded.check_granularity_same,
                 aliases=excluded.aliases,
                 biz_line=excluded.biz_line,
                 theme_domain=excluded.theme_domain,
                 biz_object=excluded.biz_object,
                 biz_process=excluded.biz_process""",
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


# ---------- Analysis dimension binds (meta_field ids) ----------

def list_metric_dim_binds(metric_type: str, metric_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT f.id, f.field_name AS code, f.display_name AS name,
                      f.semantic_role,
                      CASE WHEN f.semantic_role='grain' THEN 'grain' ELSE 'attr' END AS dim_role,
                      f.enabled, f.sort_no, f.is_analysis_dim,
                      t.physical_name AS source_table, f.field_name AS source_field,
                      t.table_kind AS origin, t.name AS table_name
               FROM metric_dim_bind b
               JOIN meta_field f ON f.id = b.dim_id
               JOIN meta_table t ON t.id = f.table_id
               WHERE b.metric_type=? AND b.metric_id=?
               ORDER BY CASE f.semantic_role WHEN 'grain' THEN 0 ELSE 1 END,
                        f.sort_no, f.field_name""",
            (metric_type, metric_id),
        ).fetchall()
    return [dict(r) for r in rows]


def list_metric_dim_ids(metric_type: str, metric_id: str) -> list[str]:
    return [r["id"] for r in list_metric_dim_binds(metric_type, metric_id)]


def metric_bound_dim_labels(metric_type: str, metric_id: str) -> str:
    rows = list_metric_dim_binds(metric_type, metric_id)
    if not rows:
        return "-"
    parts = []
    for r in rows:
        tag = "粒" if r["dim_role"] == "grain" else "析"
        origin = "fact" if r.get("origin") == "fact" else "dim"
        parts.append(f"{r['name']}[{tag}/{origin}]")
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
    roles = {
        r["id"]: r["semantic_role"]
        for r in conn.execute(
            f"SELECT id, semantic_role FROM meta_field WHERE id IN ({','.join('?'*len(dim_ids))})",
            dim_ids,
        )
    }
    if len(roles) != len(set(dim_ids)):
        raise ValueError("存在无效字段 ID（请在元数据中勾选「用于分析维度」）")
    if metric_type in ("atomic", "derived") and not any(
        roles[i] == "grain" for i in dim_ids
    ):
        raise ValueError("原子/派生指标必须至少绑定一个「粒度」字段")
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
        f"""SELECT field_name AS code FROM meta_field
            WHERE id IN ({','.join('?'*len(dim_ids))}) AND semantic_role='grain'
            ORDER BY sort_no""",
        dim_ids,
    ).fetchall()
    codes = [r["code"] for r in rows]
    return codes or ["project_number"]

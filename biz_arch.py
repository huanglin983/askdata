"""Business architecture catalog: 业务线 → 主题域 → 业务对象 → 业务过程."""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any

import db

NODE_TYPES = ("line", "domain", "object", "process")

NODE_TYPE_LABEL = {
    "line": "业务线",
    "domain": "主题域",
    "object": "业务对象",
    "process": "业务过程",
}

# child type allowed under parent type (None = root)
CHILD_TYPE = {
    None: "line",
    "line": "domain",
    "domain": "object",
    "object": "process",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS biz_arch (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            node_type TEXT NOT NULL,
            name_zh TEXT NOT NULL,
            name_en TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            sort_no INTEGER NOT NULL DEFAULT 100,
            remark TEXT,
            FOREIGN KEY (parent_id) REFERENCES biz_arch(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_biz_arch_parent ON biz_arch(parent_id);
        CREATE INDEX IF NOT EXISTS idx_biz_arch_type ON biz_arch(node_type);
        """
    )


def seed_demo(conn: sqlite3.Connection) -> None:
    """Seed 支架 → 财务 → 项目 → 五阶段业务过程."""
    n = conn.execute("SELECT COUNT(*) AS c FROM biz_arch").fetchone()["c"]
    if n:
        return

    line_id = "ba_line_bracket"
    domain_id = "ba_domain_finance"
    object_id = "ba_object_project"
    rows = [
        (line_id, None, "line", "支架", "BRACKET", 1, 10, "支架业务线"),
        (domain_id, line_id, "domain", "财务", "FINANCE", 1, 10, "财务主题域"),
        (object_id, domain_id, "object", "项目", "PROJECT", 1, 10, "项目业务对象"),
    ]
    for i, (stage, _amount, _rate) in enumerate(db.STAGES):
        rows.append(
            (
                f"ba_proc_{stage.lower()}",
                object_id,
                "process",
                stage,
                stage.upper() if stage != "FinalCST" else "FINALCST",
                1,
                (i + 1) * 10,
                f"{stage} 阶段业务过程",
            )
        )
    conn.executemany(
        """INSERT INTO biz_arch
           (id, parent_id, node_type, name_zh, name_en, enabled, sort_no, remark)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["type_label"] = NODE_TYPE_LABEL.get(d.get("node_type") or "", d.get("node_type"))
    return d


def get_node(node_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM biz_arch WHERE id=?", (node_id,)
        ).fetchone()
    return _row_dict(row)


def list_children(
    parent_id: str | None = None,
    *,
    keyword: str = "",
    enabled: str | None = None,
) -> list[dict]:
    """List direct children; parent_id=None means top-level 业务线."""
    sql = "SELECT * FROM biz_arch WHERE "
    params: list[Any] = []
    if parent_id:
        sql += "parent_id=?"
        params.append(parent_id)
    else:
        sql += "parent_id IS NULL"
    if keyword.strip():
        sql += " AND (name_zh LIKE ? OR name_en LIKE ? OR IFNULL(remark,'') LIKE ?)"
        like = f"%{keyword.strip()}%"
        params.extend([like, like, like])
    if enabled in ("0", "1"):
        sql += " AND enabled=?"
        params.append(int(enabled))
    sql += " ORDER BY sort_no, name_zh"
    with db.get_conn() as conn:
        rows = list(conn.execute(sql, params))
        out = []
        for r in rows:
            item = _row_dict(r)
            assert item is not None
            item["child_count"] = conn.execute(
                "SELECT COUNT(*) AS c FROM biz_arch WHERE parent_id=?",
                (r["id"],),
            ).fetchone()["c"]
            item["line_name"] = _ancestor_name(conn, r["id"], "line")
            item["domain_name"] = _ancestor_name(conn, r["id"], "domain")
            item["object_name"] = _ancestor_name(conn, r["id"], "object")
            out.append(item)
        return out


def _ancestor_name(
    conn: sqlite3.Connection, node_id: str, want_type: str
) -> str:
    """Walk up to find name of ancestor with given type (or self)."""
    cur = conn.execute("SELECT * FROM biz_arch WHERE id=?", (node_id,)).fetchone()
    while cur:
        if cur["node_type"] == want_type:
            return cur["name_zh"] or ""
        if not cur["parent_id"]:
            break
        cur = conn.execute(
            "SELECT * FROM biz_arch WHERE id=?", (cur["parent_id"],)
        ).fetchone()
    return ""


def build_tree(enabled_only: bool = False) -> list[dict]:
    """Nested tree for left directory."""
    sql = "SELECT * FROM biz_arch"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort_no, name_zh"
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql)]
    by_parent: dict[str | None, list[dict]] = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    counts = {
        r["id"]: sum(1 for x in rows if x["parent_id"] == r["id"]) for r in rows
    }

    def nest(parent_id: str | None) -> list[dict]:
        out = []
        for r in by_parent.get(parent_id, []):
            node = dict(r)
            node["type_label"] = NODE_TYPE_LABEL.get(r["node_type"], r["node_type"])
            node["child_count"] = counts.get(r["id"], 0)
            node["children"] = nest(r["id"])
            out.append(node)
        return out

    return nest(None)


def path_names(node_id: str) -> dict[str, str]:
    """Return {biz_line, theme_domain, biz_object, biz_process} for a node."""
    out = {
        "biz_line": "",
        "theme_domain": "",
        "biz_object": "",
        "biz_process": "",
    }
    key_map = {
        "line": "biz_line",
        "domain": "theme_domain",
        "object": "biz_object",
        "process": "biz_process",
    }
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM biz_arch WHERE id=?", (node_id,)
        ).fetchone()
        while cur:
            k = key_map.get(cur["node_type"])
            if k:
                out[k] = cur["name_zh"] or ""
            if not cur["parent_id"]:
                break
            cur = conn.execute(
                "SELECT * FROM biz_arch WHERE id=?", (cur["parent_id"],)
            ).fetchone()
    return out


def list_options(node_type: str, parent_id: str | None = None) -> list[dict]:
    """Options for cascading selects (enabled only)."""
    sql = "SELECT * FROM biz_arch WHERE node_type=? AND enabled=1"
    params: list[Any] = [node_type]
    if parent_id:
        sql += " AND parent_id=?"
        params.append(parent_id)
    elif node_type != "line":
        # without parent filter still allow all of type (for loose selects)
        pass
    sql += " ORDER BY sort_no, name_zh"
    with db.get_conn() as conn:
        return [_row_dict(r) for r in conn.execute(sql, params)]


def taxonomy_catalog() -> dict[str, list[dict]]:
    """All enabled nodes grouped by type, with parent_id for cascading UI."""
    with db.get_conn() as conn:
        rows = list(
            conn.execute(
                """SELECT * FROM biz_arch WHERE enabled=1
                   ORDER BY sort_no, name_zh"""
            )
        )
    return {
        t: [_row_dict(r) for r in rows if r["node_type"] == t] for t in NODE_TYPES
    }


def upsert_node(data: dict) -> str:
    nid = (data.get("id") or "").strip() or f"ba_{uuid.uuid4().hex[:10]}"
    parent_id = (data.get("parent_id") or "").strip() or None
    node_type = (data.get("node_type") or "").strip()
    name_zh = (data.get("name_zh") or "").strip()
    name_en = (data.get("name_en") or "").strip()
    if node_type not in NODE_TYPES:
        raise ValueError(f"非法节点类型: {node_type}")
    if not name_zh:
        raise ValueError("中文名称不能为空")

    with db.get_conn() as conn:
        parent = None
        if parent_id:
            parent = conn.execute(
                "SELECT * FROM biz_arch WHERE id=?", (parent_id,)
            ).fetchone()
            if not parent:
                raise ValueError(f"父节点不存在: {parent_id}")
            expected = CHILD_TYPE.get(parent["node_type"])
            if expected != node_type:
                raise ValueError(
                    f"{NODE_TYPE_LABEL[parent['node_type']]}下只能新增"
                    f"{NODE_TYPE_LABEL.get(expected or '', expected)}"
                )
        else:
            if node_type != "line":
                raise ValueError("顶级只能新增业务线")

        # uniqueness under same parent
        dup = conn.execute(
            """SELECT id FROM biz_arch
               WHERE IFNULL(parent_id,'')=? AND name_zh=? AND id<>?""",
            (parent_id or "", name_zh, nid),
        ).fetchone()
        if dup:
            raise ValueError(f"同级已存在中文名「{name_zh}」")

        conn.execute(
            """INSERT INTO biz_arch
               (id, parent_id, node_type, name_zh, name_en, enabled, sort_no, remark)
               VALUES (:id,:parent_id,:node_type,:name_zh,:name_en,:enabled,:sort_no,:remark)
               ON CONFLICT(id) DO UPDATE SET
                 parent_id=excluded.parent_id,
                 node_type=excluded.node_type,
                 name_zh=excluded.name_zh,
                 name_en=excluded.name_en,
                 enabled=excluded.enabled,
                 sort_no=excluded.sort_no,
                 remark=excluded.remark""",
            {
                "id": nid,
                "parent_id": parent_id,
                "node_type": node_type,
                "name_zh": name_zh,
                "name_en": name_en,
                "enabled": int(data.get("enabled", 1)),
                "sort_no": int(data.get("sort_no") or 100),
                "remark": data.get("remark") or "",
            },
        )
        conn.commit()
    return nid


def set_enabled(node_ids: list[str], enabled: int) -> int:
    if not node_ids:
        return 0
    placeholders = ",".join("?" * len(node_ids))
    with db.get_conn() as conn:
        cur = conn.execute(
            f"UPDATE biz_arch SET enabled=? WHERE id IN ({placeholders})",
            [int(enabled), *node_ids],
        )
        conn.commit()
        return cur.rowcount


def delete_nodes(node_ids: list[str]) -> int:
    """Delete nodes (CASCADE children). Refuse if metrics still reference names."""
    if not node_ids:
        return 0
    with db.get_conn() as conn:
        deleted = 0
        for nid in node_ids:
            node = conn.execute(
                "SELECT * FROM biz_arch WHERE id=?", (nid,)
            ).fetchone()
            if not node:
                continue
            # gather self + descendants names by type for soft check
            names = _collect_subtree_names(conn, nid)
            refs = _metric_refs(conn, names)
            if refs:
                raise ValueError(
                    f"「{node['name_zh']}」仍被指标引用，无法删除：{', '.join(refs[:5])}"
                    + ("…" if len(refs) > 5 else "")
                )
            conn.execute("DELETE FROM biz_arch WHERE id=?", (nid,))
            deleted += 1
        conn.commit()
        return deleted


def _collect_subtree_names(
    conn: sqlite3.Connection, root_id: str
) -> dict[str, set[str]]:
    by_type: dict[str, set[str]] = {t: set() for t in NODE_TYPES}
    stack = [root_id]
    while stack:
        cur_id = stack.pop()
        row = conn.execute(
            "SELECT * FROM biz_arch WHERE id=?", (cur_id,)
        ).fetchone()
        if not row:
            continue
        by_type[row["node_type"]].add(row["name_zh"])
        for c in conn.execute(
            "SELECT id FROM biz_arch WHERE parent_id=?", (cur_id,)
        ):
            stack.append(c["id"])
    return by_type


def _metric_refs(conn: sqlite3.Connection, names: dict[str, set[str]]) -> list[str]:
    """Return metric names that reference any of the architecture names."""
    col_map = {
        "line": "biz_line",
        "domain": "theme_domain",
        "object": "biz_object",
        "process": "biz_process",
    }
    hit: list[str] = []
    for ntype, col in col_map.items():
        vals = [v for v in names.get(ntype, set()) if v]
        if not vals:
            continue
        ph = ",".join("?" * len(vals))
        for table in ("metric_atomic", "metric_derived", "metric_composite"):
            # column may not exist on very old DBs; ignore
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols or "name" not in cols:
                continue
            for r in conn.execute(
                f"SELECT name FROM {table} WHERE {col} IN ({ph})", vals
            ):
                hit.append(r["name"])
    # dedupe preserve order
    seen: set[str] = set()
    out = []
    for n in hit:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def default_child_type(parent_id: str | None) -> str:
    if not parent_id:
        return "line"
    parent = get_node(parent_id)
    if not parent:
        return "line"
    return CHILD_TYPE.get(parent["node_type"]) or "line"

"""Table model map: fact/dim cards + field-level join edges."""
from __future__ import annotations

from typing import Any

import meta

# Distinct stroke colors for multi-field joins within one relationship
EDGE_COLORS = [
    "#0f766e",  # teal
    "#c2410c",  # orange
    "#1d4ed8",  # blue
    "#7e22ce",  # purple
    "#b45309",  # amber
    "#be123c",  # rose
]


def _infer_cardinality(dim_grain: list[str], join_rights: list[str], fact_grain: list[str], join_lefts: list[str]) -> str:
    """Return '1:1' or '1:N' (fact→dim reading as 1对1 / 1对多).

    - 1:1 when join keys cover both sides' grain (unique lookup both ways)
    - 1:N otherwise (typical star: many fact rows share one dim row)
    """
    dim_set = set(dim_grain)
    right_set = set(join_rights)
    fact_set = set(fact_grain)
    left_set = set(join_lefts)
    dim_unique = bool(dim_set) and dim_set <= right_set
    fact_unique = bool(fact_set) and fact_set <= left_set
    if dim_unique and fact_unique:
        return "1:1"
    return "1:N"


def _cardinality_label(code: str) -> str:
    return "1对1" if code == "1:1" else "1对多"


def _field_dict(row: Any, grain_names: set[str], join_names: set[str]) -> dict:
    name = row["field_name"]
    return {
        "id": row["id"],
        "name": name,
        "display_name": row["display_name"] or name,
        "is_pk": bool(row["is_pk"]),
        "is_grain": name in grain_names or (row["semantic_role"] or "") == "grain",
        "is_join": name in join_names,
        "enabled": bool(row["enabled"]),
        "semantic_role": row["semantic_role"] or "other",
    }


def _table_node(table: Any, join_field_names: set[str]) -> dict:
    tid = table["id"]
    grain = meta.get_table_grain_keys(tid)
    grain_set = set(grain)
    fields_raw = meta.list_meta_fields(tid)
    fields = [
        _field_dict(f, grain_set, join_field_names)
        for f in fields_raw
        if f["enabled"]
    ]
    # Default visible: grain + join keys (so edges can attach when collapsed)
    default_names = grain_set | join_field_names
    if not default_names:
        # fallback: physical PKs
        default_names = {f["name"] for f in fields if f["is_pk"]}
    kind = table["table_kind"] or "dim"
    return {
        "id": tid,
        "name": table["name"],
        "physical_name": table["physical_name"],
        "kind": kind,
        "kind_label": "事实表" if kind == "fact" else "维度表",
        "enabled": bool(table["enabled"]),
        "grain_keys": grain,
        "fields": fields,
        "default_field_names": sorted(default_names),
        "edit_url": f"/meta/tables/edit/{tid}",
    }


def build_table_model_map() -> dict:
    """Build groups (per fact) of table nodes + field-level edges."""
    tables = [dict(t) for t in meta.list_meta_tables()]
    by_id = {t["id"]: t for t in tables}
    rels = [r for r in meta.list_meta_rels() if r.get("enabled")]

    # Collect join field names per table
    join_fields: dict[str, set[str]] = {t["id"]: set() for t in tables}
    for r in rels:
        for k in r.get("join_keys_list") or []:
            left = (k.get("left") or "").strip()
            right = (k.get("right") or "").strip()
            if left:
                join_fields.setdefault(r["fact_table_id"], set()).add(left)
            if right:
                join_fields.setdefault(r["dim_table_id"], set()).add(right)

    nodes: dict[str, dict] = {}
    for t in tables:
        if not t.get("enabled"):
            continue
        nodes[t["id"]] = _table_node(t, join_fields.get(t["id"], set()))

    groups: list[dict] = []
    used_dims: set[str] = set()
    fact_ids = [t["id"] for t in tables if t.get("enabled") and t.get("table_kind") == "fact"]

    for fact_id in fact_ids:
        if fact_id not in nodes:
            continue
        fact_rels = [r for r in rels if r["fact_table_id"] == fact_id]
        dim_ids: list[str] = []
        edges: list[dict] = []
        fact_grain = nodes[fact_id]["grain_keys"]

        for r in fact_rels:
            dim_id = r["dim_table_id"]
            if dim_id not in nodes:
                continue
            if dim_id not in dim_ids:
                dim_ids.append(dim_id)
            used_dims.add(dim_id)
            keys = r.get("join_keys_list") or []
            lefts = [(k.get("left") or "").strip() for k in keys]
            rights = [(k.get("right") or "").strip() for k in keys]
            lefts = [x for x in lefts if x]
            rights = [x for x in rights if x]
            card = _infer_cardinality(
                nodes[dim_id]["grain_keys"],
                rights,
                fact_grain,
                lefts,
            )
            for i, k in enumerate(keys):
                left = (k.get("left") or "").strip()
                right = (k.get("right") or "").strip()
                if not left or not right:
                    continue
                edges.append(
                    {
                        "id": f"{r['id']}__{i}",
                        "rel_id": r["id"],
                        "fact_table_id": fact_id,
                        "dim_table_id": dim_id,
                        "left_field": left,
                        "right_field": right,
                        "join_type": r.get("join_type") or "LEFT",
                        "cardinality": card,
                        "cardinality_label": _cardinality_label(card),
                        "color": EDGE_COLORS[i % len(EDGE_COLORS)],
                        "key_index": i,
                        "key_count": len(keys),
                        "edit_url": f"/meta/rels/edit/{r['id']}",
                    }
                )

        groups.append(
            {
                "fact_id": fact_id,
                "fact": nodes[fact_id],
                "dims": [nodes[d] for d in dim_ids],
                "edges": edges,
            }
        )

    # Orphan dims (enabled, no enabled rel) and unused facts already covered
    orphan_dims = [
        nodes[t["id"]]
        for t in tables
        if t.get("enabled")
        and t.get("table_kind") == "dim"
        and t["id"] in nodes
        and t["id"] not in used_dims
    ]
    orphan_facts = [
        nodes[fid]
        for fid in fact_ids
        if fid in nodes and not any(g["fact_id"] == fid and g["edges"] for g in groups)
    ]
    # Keep empty-edge fact groups only if no dims; still show fact alone via orphan_facts
    groups = [g for g in groups if g["edges"] or g["dims"]]

    stats = {
        "facts": len([n for n in nodes.values() if n["kind"] == "fact"]),
        "dims": len([n for n in nodes.values() if n["kind"] == "dim"]),
        "rels": len(rels),
        "edges": sum(len(g["edges"]) for g in groups),
        "orphans": len(orphan_dims) + len(orphan_facts),
    }
    return {
        "groups": groups,
        "orphan_dims": orphan_dims,
        "orphan_facts": orphan_facts,
        "stats": stats,
        "nodes": list(nodes.values()),
    }

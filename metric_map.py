"""Metric dependency map: composite → derived/atomic trees + orphans."""
from __future__ import annotations

import json
from typing import Any

import db


TYPE_LABEL = {"composite": "复合", "derived": "派生", "atomic": "原子"}


def _node(
    *,
    metric_id: str,
    name: str,
    mtype: str,
    detail: str = "",
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": metric_id,
        "name": name,
        "type": mtype,
        "type_label": TYPE_LABEL.get(mtype, mtype),
        "detail": detail,
        "children": children or [],
    }


def _atomic_node(row: Any) -> dict[str, Any]:
    parts = []
    if row["source_field"]:
        parts.append(row["source_field"])
    if row["stage_type"]:
        parts.append(row["stage_type"])
    if row["rate_col"]:
        parts.append(f"汇率 {row['rate_col']}")
    return _node(
        metric_id=row["id"],
        name=row["name"],
        mtype="atomic",
        detail=" · ".join(parts) or (row["remark"] or ""),
    )


def _derived_node(row: Any, *, with_children: bool = True) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    detail_parts = []
    if row["stage_type"]:
        detail_parts.append(row["stage_type"])
    atomic = db.get_atomic(row["atomic_id"]) if row["atomic_id"] else None
    if atomic:
        detail_parts.append(f"← {atomic['name']}")
        if with_children:
            children.append(_atomic_node(atomic))
    return _node(
        metric_id=row["id"],
        name=row["name"],
        mtype="derived",
        detail=" · ".join(detail_parts),
        children=children,
    )


def _composite_node(
    row: Any,
    *,
    visiting: set[str] | None = None,
    with_children: bool = True,
) -> dict[str, Any]:
    visiting = visiting or set()
    children: list[dict[str, Any]] = []
    if with_children and row["id"] not in visiting:
        visiting.add(row["id"])
        for sid in json.loads(row["sub_metric_ids"] or "[]"):
            child = _expand(sid, visiting=visiting)
            if child:
                children.append(child)
        visiting.discard(row["id"])
    return _node(
        metric_id=row["id"],
        name=row["name"],
        mtype="composite",
        detail=row["formula"] or "",
        children=children,
    )


def _expand(metric_id: str, *, visiting: set[str]) -> dict[str, Any] | None:
    c = db.get_composite(metric_id)
    if c:
        return _composite_node(c, visiting=visiting, with_children=True)
    d = db.get_derived(metric_id)
    if d:
        return _derived_node(d, with_children=True)
    a = db.get_atomic(metric_id)
    if a:
        return _atomic_node(a)
    return None


def _collect_ids(node: dict[str, Any], out: set[str]) -> None:
    out.add(node["id"])
    for ch in node.get("children") or []:
        _collect_ids(ch, out)


def build_metric_map() -> dict[str, Any]:
    """Return root trees (composite-first) and orphan metrics.

    Trees: composites not referenced by another composite, expanded downward.
    Orphans: metrics never appearing in any tree (flat cards; derived still
    optionally link their atomic if that atomic is also orphan-only — shown
    as mini parent→child under orphan derived).
    """
    composites = list(db.list_composite())
    derived = list(db.list_derived())
    atomics = list(db.list_atomic())

    referenced_by_composite: set[str] = set()
    for c in composites:
        for sid in json.loads(c["sub_metric_ids"] or "[]"):
            referenced_by_composite.add(sid)

    roots = [c for c in composites if c["id"] not in referenced_by_composite]
    # stable: keep list order; nested composites appear only as children
    trees: list[dict[str, Any]] = []
    in_trees: set[str] = set()
    for c in roots:
        node = _composite_node(c, visiting=set(), with_children=True)
        trees.append(node)
        _collect_ids(node, in_trees)

    # Orphan composites that somehow weren't roots (shouldn't happen) — skip if in trees
    orphan_nodes: list[dict[str, Any]] = []

    for c in composites:
        if c["id"] not in in_trees:
            orphan_nodes.append(
                _composite_node(c, visiting=set(), with_children=False)
            )

    for d in derived:
        if d["id"] not in in_trees:
            # include atomic child only if that atomic is also unused in trees
            atomic = db.get_atomic(d["atomic_id"]) if d["atomic_id"] else None
            include_atomic = bool(atomic and atomic["id"] not in in_trees)
            node = _derived_node(d, with_children=include_atomic)
            orphan_nodes.append(node)
            if include_atomic and atomic:
                in_trees.add(atomic["id"])  # avoid duplicating under flat atomics
            in_trees.add(d["id"])

    for a in atomics:
        if a["id"] not in in_trees:
            orphan_nodes.append(_atomic_node(a))

    return {
        "trees": trees,
        "orphans": orphan_nodes,
        "stats": {
            "trees": len(trees),
            "orphans": len(orphan_nodes),
            "composite": len(composites),
            "derived": len(derived),
            "atomic": len(atomics),
        },
    }

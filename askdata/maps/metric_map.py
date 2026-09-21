"""Metric dependency map: biz-arch roots → composite/derived → atomic + orphans."""
from __future__ import annotations

import json
from typing import Any

from askdata.infra import db
from askdata.meta import biz_arch

TYPE_LABEL = {
    "composite": "复合",
    "derived": "派生",
    "atomic": "原子",
    "unclassified": "未分类",
    **biz_arch.NODE_TYPE_LABEL,
}

ARCH_TYPES = frozenset(biz_arch.NODE_TYPES)
UNCLASSIFIED_ID = "_unclassified"


def _node(
    *,
    metric_id: str,
    name: str,
    mtype: str,
    detail: str = "",
    children: list[dict[str, Any]] | None = None,
    edit_url: str | None = None,
) -> dict[str, Any]:
    return {
        "id": metric_id,
        "name": name,
        "type": mtype,
        "type_label": TYPE_LABEL.get(mtype, mtype),
        "detail": detail,
        "children": children or [],
        "is_arch": mtype in ARCH_TYPES or mtype == "unclassified",
        "edit_url": edit_url,
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
    if not node.get("is_arch"):
        out.add(node["id"])
    for ch in node.get("children") or []:
        _collect_ids(ch, out)


def _row_tax(row: Any) -> tuple[str, str, str, str]:
    return (
        (row["biz_line"] or "").strip(),
        (row["theme_domain"] or "").strip(),
        (row["biz_object"] or "").strip(),
        (row["biz_process"] or "").strip(),
    )


def _resolve_metric_tax(row: Any, mtype: str, *, visiting: set[str] | None = None) -> tuple[str, str, str, str]:
    """Best-effort taxonomy: stored fields → stage → related metrics."""
    visiting = visiting or set()
    mid = row["id"]
    if mid in visiting:
        return ("", "", "", "")
    visiting.add(mid)

    line, domain, obj, proc = _row_tax(row)
    if mtype == "derived" and not proc:
        proc = (row["stage_type"] or "").strip()
    if mtype == "atomic" and not proc:
        proc = (row["stage_type"] or "").strip()

    if not any((line, domain, obj, proc)):
        if mtype == "derived" and row["atomic_id"]:
            atomic = db.get_atomic(row["atomic_id"])
            if atomic:
                line, domain, obj, proc = _resolve_metric_tax(
                    atomic, "atomic", visiting=visiting
                )
        elif mtype == "composite":
            for sid in json.loads(row["sub_metric_ids"] or "[]"):
                child_row = db.get_composite(sid)
                ctype = "composite"
                if not child_row:
                    child_row = db.get_derived(sid)
                    ctype = "derived"
                if not child_row:
                    child_row = db.get_atomic(sid)
                    ctype = "atomic"
                if not child_row:
                    continue
                line, domain, obj, proc = _resolve_metric_tax(
                    child_row, ctype, visiting=visiting
                )
                if any((line, domain, obj, proc)):
                    break

    visiting.discard(mid)
    return (line, domain, obj, proc)


def _find_bucket(
    process_index: dict[tuple[str, str, str, str], dict],
    tax: tuple[str, str, str, str],
    unclassified: dict[str, Any],
) -> dict[str, Any]:
    if tax in process_index and tax[3]:
        return process_index[tax]
    proc = tax[3]
    if not proc:
        return unclassified
    candidates = [
        (key, node)
        for key, node in process_index.items()
        if key[3] == proc
        and (not tax[0] or key[0] == tax[0])
        and (not tax[1] or key[1] == tax[1])
        and (not tax[2] or key[2] == tax[2])
    ]
    if candidates:
        return candidates[0][1]
    return unclassified


def _arch_map_node(arch: dict[str, Any]) -> dict[str, Any]:
    detail_parts = []
    if arch.get("name_en"):
        detail_parts.append(arch["name_en"])
    if arch.get("remark"):
        detail_parts.append(arch["remark"])
    return _node(
        metric_id=arch["id"],
        name=arch["name_zh"],
        mtype=arch["node_type"],
        detail=" · ".join(detail_parts),
        children=[],
        edit_url=f"/meta/biz-arch/edit/{arch['id']}",
    )


def _build_arch_forest() -> tuple[list[dict[str, Any]], dict[tuple[str, str, str, str], dict]]:
    """Convert biz_arch catalog into map nodes; index process leaves by taxonomy path."""
    raw = biz_arch.build_tree(enabled_only=True)
    process_index: dict[tuple[str, str, str, str], dict] = {}

    def convert(
        nodes: list[dict[str, Any]],
        path: tuple[str, str, str, str] = ("", "", "", ""),
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for arch in nodes:
            node = _arch_map_node(arch)
            ntype = arch["node_type"]
            line, domain, obj, proc = path
            name = arch["name_zh"] or ""
            if ntype == "line":
                next_path = (name, "", "", "")
            elif ntype == "domain":
                next_path = (line, name, "", "")
            elif ntype == "object":
                next_path = (line, domain, name, "")
            else:  # process
                next_path = (line, domain, obj, name)
                process_index[next_path] = node
            node["children"] = convert(arch.get("children") or [], next_path)
            out.append(node)
        return out

    return convert(raw), process_index


def _unclassified_root() -> dict[str, Any]:
    return _node(
        metric_id=UNCLASSIFIED_ID,
        name="未分类",
        mtype="unclassified",
        detail="未配置完整业务线 / 主题域 / 业务对象 / 业务过程",
        children=[],
    )


def _prune_empty_arch(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop architecture branches that contain no metric descendants."""
    kept: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("is_arch") and node["type"] != "unclassified":
            node["children"] = _prune_empty_arch(node.get("children") or [])
            if node["children"]:
                kept.append(node)
        else:
            kept.append(node)
    return kept


def _sort_metric_children(node: dict[str, Any]) -> None:
    """Arch children first (already ordered), then composite, then derived, then atomic."""
    kids = node.get("children") or []
    arch = [c for c in kids if c.get("is_arch")]
    metrics = [c for c in kids if not c.get("is_arch")]
    order = {"composite": 0, "derived": 1, "atomic": 2}
    metrics.sort(key=lambda c: (order.get(c["type"], 9), c["name"], c["id"]))
    node["children"] = arch + metrics
    for ch in arch:
        _sort_metric_children(ch)


def _direct_parents_index() -> dict[str, list[tuple[str, Any]]]:
    """metric_id → [(parent_type, parent_row), ...] that directly reference it."""
    index: dict[str, list[tuple[str, Any]]] = {}
    for d in db.list_derived():
        aid = (d["atomic_id"] or "").strip()
        if aid:
            index.setdefault(aid, []).append(("derived", d))
    for c in db.list_composite():
        for sid in json.loads(c["sub_metric_ids"] or "[]"):
            sid = (sid or "").strip()
            if sid:
                index.setdefault(sid, []).append(("composite", c))
    return index


def _reverse_parents(
    metric_id: str,
    parents_index: dict[str, list[tuple[str, Any]]],
    *,
    visiting: set[str],
) -> list[dict[str, Any]]:
    """Upstream consumers of metric_id (derived / composite), nested upward."""
    if metric_id in visiting:
        return []
    visiting.add(metric_id)
    out: list[dict[str, Any]] = []
    for ptype, prow in parents_index.get(metric_id, []):
        pid = prow["id"]
        if ptype == "derived":
            detail_parts = []
            if prow["stage_type"]:
                detail_parts.append(prow["stage_type"])
            detail_parts.append("引用本原子")
            node = _node(
                metric_id=pid,
                name=prow["name"],
                mtype="derived",
                detail=" · ".join(detail_parts),
                children=_reverse_parents(pid, parents_index, visiting=visiting),
            )
        else:
            node = _node(
                metric_id=pid,
                name=prow["name"],
                mtype="composite",
                detail=prow["formula"] or "",
                children=_reverse_parents(pid, parents_index, visiting=visiting),
            )
        out.append(node)
    visiting.discard(metric_id)
    order = {"derived": 0, "composite": 1}
    out.sort(key=lambda n: (order.get(n["type"], 9), n["name"], n["id"]))
    return out


def _reverse_tree_from_atomic(row: Any) -> dict[str, Any]:
    """Atomic as root; children = derived / composite that (transitively) reference it."""
    root = _atomic_node(row)
    root["reverse"] = True
    root["children"] = _reverse_parents(
        row["id"], _direct_parents_index(), visiting=set()
    )
    return root


def build_subtree(metric_id: str) -> dict[str, Any] | None:
    """Expand a single metric for the map panel.

    - Atomic: reverse tree (who references this atomic).
    - Composite / derived: downward dependency tree (unchanged).
    """
    mid = (metric_id or "").strip()
    if not mid:
        return None
    atomic = db.get_atomic(mid)
    if atomic:
        return _reverse_tree_from_atomic(atomic)
    return _expand(mid, visiting=set())


def build_metric_map() -> dict[str, Any]:
    """Return biz-arch rooted trees and orphan atomics.

    Roots: 业务线 → 主题域 → 业务对象 → 业务过程, with every composite and
    derived metric hung under its taxonomy path (expanded downward to atomics).
    Metrics missing a matching process path go under「未分类」.
    Orphans: atomics never appearing in any expanded tree.
    """
    composites = list(db.list_composite())
    derived = list(db.list_derived())
    atomics = list(db.list_atomic())

    forest, process_index = _build_arch_forest()
    unclassified = _unclassified_root()

    for c in composites:
        node = _composite_node(c, visiting=set(), with_children=True)
        tax = _resolve_metric_tax(c, "composite")
        _find_bucket(process_index, tax, unclassified)["children"].append(node)

    for d in derived:
        node = _derived_node(d, with_children=True)
        tax = _resolve_metric_tax(d, "derived")
        _find_bucket(process_index, tax, unclassified)["children"].append(node)

    trees = _prune_empty_arch(forest)
    if unclassified["children"]:
        _sort_metric_children(unclassified)
        trees.append(unclassified)

    for t in trees:
        _sort_metric_children(t)

    in_trees: set[str] = set()
    for t in trees:
        _collect_ids(t, in_trees)

    orphan_nodes: list[dict[str, Any]] = []
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

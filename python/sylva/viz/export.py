"""Graph export for the visualisation UI.

Reads the Sylva graph database directly (stdlib sqlite3 — no Rust) and produces
a viz-shaped `{nodes, links}` structure: nodes are symbols (with kind, file,
coverage, and degree for sizing/colouring), links are `calls`/`imports` edges.

`build_graph` returns the dict (used live by the server); `export_graph_json`
writes it to `visualisation/graph.json` as the on-disk artifact refreshed at
analysis time or on demand.

Kept deliberately viz-shaped and separate from Feature 6.3's general
JSON/GraphML export for external tools (Gephi/Obsidian).
"""

import json
import os
import sqlite3


def coverage_state(coverage):
    """Map a coverage percentage to a discrete overlay band.

    Drives the UI's red->amber->green->grey colouring at the data layer so the
    overlay is testable without rendering. `None` (no data) -> 'none' (grey);
    0 -> 'uncovered' (red); 100 -> 'covered' (green); in between is banded.
    """
    if coverage is None:
        return "none"
    if coverage <= 0:
        return "uncovered"
    if coverage >= 100:
        return "covered"
    if coverage < 50:
        return "low"
    if coverage < 80:
        return "partial"
    return "high"


def build_graph(db_path):
    """Build the `{nodes, links}` graph dict from the database.

    Raises FileNotFoundError if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        symbols = conn.execute(
            "SELECT s.id, s.name, s.kind, s.line_start, s.coverage_pct, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id"
        ).fetchall()
        edges = conn.execute(
            "SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('calls', 'imports')"
        ).fetchall()
    finally:
        conn.close()

    degree = {}
    for src, dst, _kind in edges:
        degree[src] = degree.get(src, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1

    nodes = [
        {
            "id": sid,
            "name": name,
            "kind": kind,
            "file": path,
            "line": line,
            "coverage": coverage,  # may be None
            "coverage_state": coverage_state(coverage),  # 4.5 overlay band
            "degree": degree.get(sid, 0),
        }
        for (sid, name, kind, line, coverage, path) in symbols
    ]
    links = [{"source": src, "target": dst, "kind": kind} for (src, dst, kind) in edges]

    # Feature 4.6 — module (file) clustering: a higher-level view. Each file is a
    # module; edges between modules aggregate the cross-module symbol edges.
    module_of = {n["id"]: n["file"] for n in nodes}
    symbols_per_module = {}
    for n in nodes:
        symbols_per_module[n["file"]] = symbols_per_module.get(n["file"], 0) + 1
    modules = [
        {"id": path, "symbols": count}
        for path, count in sorted(symbols_per_module.items())
    ]

    module_edges = {}
    for src, dst, _kind in edges:
        sm, tm = module_of.get(src), module_of.get(dst)
        if sm is None or tm is None or sm == tm:
            continue  # intra-module (or dangling) — collapses away at module level
        module_edges[(sm, tm)] = module_edges.get((sm, tm), 0) + 1
    module_links = [
        {"source": sm, "target": tm, "weight": w}
        for (sm, tm), w in sorted(module_edges.items())
    ]

    return {
        "nodes": nodes,
        "links": links,
        "modules": modules,
        "module_links": module_links,
    }


def _bfs_layers(roots, adj):
    """Assign each node a layer = BFS depth from the nearest root. Cycles
    terminate via the visited (`layer`) set. Shared by the flowchart (4.10) and
    execution-path (4.11) views."""
    layer = {r: 0 for r in roots}
    frontier = list(roots)
    depth = 1
    while frontier:
        nxt = []
        for u in frontier:
            for v in adj.get(u, []):
                if v not in layer:
                    layer[v] = depth
                    nxt.append(v)
        frontier = nxt
        depth += 1
    return layer


def flow_layout(db_path, entry):
    """Feature 4.10 — the reachable outbound call subgraph from `entry`, as a
    layered flowchart: each node gets a `layer` (BFS depth from the entry), plus
    the call edges among the reached set. Cycles terminate via the visited set.

    Returns `{entry, nodes: [{id, name, kind, file, line, layer}], edges:
    [{source, target}]}`. An unknown entry yields empty nodes/edges.
    Raises FileNotFoundError if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        syms = conn.execute(
            "SELECT s.id, s.name, s.kind, s.line_start, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id"
        ).fetchall()
        edges = conn.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'calls'").fetchall()
    finally:
        conn.close()

    detail = {sid: (name, kind, line, path) for (sid, name, kind, line, path) in syms}
    name_ids = {}
    for sid, name, *_ in syms:
        name_ids.setdefault(name, []).append(sid)
    adj = {}
    for src, dst in edges:
        adj.setdefault(src, []).append(dst)

    roots = name_ids.get(entry, [])
    if not roots:
        return {"entry": entry, "nodes": [], "edges": []}

    layer = _bfs_layers(roots, adj)  # layer = shortest hop count from an entry root
    reached = set(layer)
    nodes = [
        {
            "id": sid,
            "name": detail[sid][0],
            "kind": detail[sid][1],
            "file": detail[sid][3],
            "line": detail[sid][2],
            "layer": layer[sid],
        }
        for sid in layer
    ]
    nodes.sort(key=lambda n: (n["layer"], n["name"]))
    flow_edges = [
        {"source": s, "target": t} for (s, t) in edges if s in reached and t in reached
    ]
    return {"entry": entry, "nodes": nodes, "edges": flow_edges}


def exec_path(db_path, test):
    """Feature 4.11 — the part of the call graph a test actually exercised, as a
    layered flowchart. The exercised symbols are the targets of the test's
    `test_covers` edges (Feature 3.3); the induced subgraph of those + the
    `calls` edges among them is layered like `flow_layout`.

    Returns `{test, nodes: [{id, name, kind, file, line, layer}], edges}`. An
    unknown/uncovered test yields empty nodes/edges. Raises FileNotFoundError if
    the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        syms = conn.execute(
            "SELECT s.id, s.name, s.kind, s.line_start, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id"
        ).fetchall()
        covered_rows = conn.execute(
            "SELECT DISTINCT e.dst_id FROM edges e JOIN symbols s ON s.id = e.src_id "
            "WHERE e.kind = 'test_covers' AND s.name = ?",
            (test,),
        ).fetchall()
        call_rows = conn.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'calls'").fetchall()
    finally:
        conn.close()

    detail = {sid: (name, kind, line, path) for (sid, name, kind, line, path) in syms}
    covered = {r[0] for r in covered_rows}
    if not covered:
        return {"test": test, "nodes": [], "edges": []}

    # Call edges restricted to the exercised (covered) set.
    adj, inbound, induced = {}, set(), []
    for src, dst in call_rows:
        if src in covered and dst in covered:
            adj.setdefault(src, []).append(dst)
            inbound.add(dst)
            induced.append((src, dst))

    roots = [c for c in covered if c not in inbound] or list(covered)  # cycle -> all roots
    layer = _bfs_layers(roots, adj)
    for c in covered:
        layer.setdefault(c, 0)  # any cycle remnant not reached -> layer 0

    nodes = [
        {
            "id": sid,
            "name": detail[sid][0],
            "kind": detail[sid][1],
            "file": detail[sid][3],
            "line": detail[sid][2],
            "layer": layer[sid],
        }
        for sid in covered
    ]
    nodes.sort(key=lambda n: (n["layer"], n["name"]))
    edges = [{"source": s, "target": d} for (s, d) in induced]
    return {"test": test, "nodes": nodes, "edges": edges}


def architecture(db_path):
    """Feature 4.8 — the architecture summary that drives the sidebar navigator.

    A thin reuse of Feature 4.3's `get_architecture` (Rust): returns
    `{modules, hubs, entry_points}` so the UI can offer clickable entry points
    and hubs to navigate the graph. No reimplementation — the ranking and
    entry-point logic live in one place.

    Raises FileNotFoundError if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    import sylva  # lazy: keeps the rest of the viz module Rust-free / importable

    return sylva.get_architecture(db_path)


def graph_version(db_path):
    """A cheap signature of the current graph state, for change detection.

    Combines symbol count, edge count, total coverage, and the latest index
    time — so it changes when symbols/edges are added or removed, when coverage
    is (re)applied, or when a file is re-indexed. Used by the live-updating UI.

    Raises FileNotFoundError if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        sym_count, cov_sum = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(coverage_pct), 0) FROM symbols"
        ).fetchone()
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        last_indexed = conn.execute(
            "SELECT COALESCE(MAX(indexed_at), '') FROM files"
        ).fetchone()[0]
    finally:
        conn.close()

    return {"version": f"{sym_count}:{edge_count}:{cov_sum}:{last_indexed}"}


def export_graph_json(db_path, out_dir="visualisation"):
    """Write the graph to `<out_dir>/graph.json`, creating the folder.

    Returns the path to the written file. Raises FileNotFoundError if the
    database does not exist.
    """
    graph = build_graph(db_path)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "graph.json")
    with open(out_path, "w") as f:
        json.dump(graph, f, indent=2)
    return out_path

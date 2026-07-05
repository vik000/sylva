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


def _accessible_ids(db_path, nodes):
    """Feature 8.3 — the set of node ids exposed by an `expose.toml` next to the
    db (Feature 8.5's executable surface). Reuses 8.5's resolution so the badge
    reflects exactly what `sylva expose` would expose. No allowlist → empty set;
    never raises (an accessibility overlay must not break graph building)."""
    toml = os.path.join(os.path.dirname(db_path) or ".", "expose.toml")
    if not os.path.isfile(toml) or not nodes:
        return set()
    try:
        from ..expose import _file_matches_module, _resolve, parse_allowlist

        with open(toml) as f:
            allowlist = parse_allowlist(f.read())
        files = [n["file"] for n in nodes]
        root = os.path.dirname(files[0]) if len(files) == 1 else os.path.commonpath(files)
        targets, _warnings = _resolve(db_path, root, allowlist)
        exposed = set()
        for module, fn, _desc in targets:
            for n in nodes:
                if n["name"] == fn and _file_matches_module(n["file"], module):
                    exposed.add(n["id"])
        return exposed
    except Exception:
        return set()


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
            "SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('calls', 'imports', 'inherits')"
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
            # Feature 5.0.1 — black-box foreign (Rust/PyO3) boundary node.
            "foreign": kind in ("foreign_module", "foreign_export"),
            # Feature 8.3 — agent-callable (exposed via 8.5's allowlist); set below.
            "accessible": False,
        }
        for (sid, name, kind, line, coverage, path) in symbols
    ]
    # Feature 8.3 — mark nodes exposed by an `expose.toml` next to the db.
    exposed = _accessible_ids(db_path, nodes)
    for n in nodes:
        n["accessible"] = n["id"] in exposed

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


def system_flow(db_path, root=None):
    """Feature 9.4 — the whole-project system-flow block diagram.

    A first-class *global* layered view: the reachable outbound call subgraph
    from the program's entrypoint, laid out top-down (reusing `flow_layout`).
    The root defaults to Feature 9.1's inferred **primary** entrypoint; each node
    is tagged `on_spine` when it lies on Feature 9.2's main spine, so the UI can
    emphasise the backbone.

    Returns `{root, nodes: [{id, name, kind, file, line, layer, on_spine}],
    edges}`. An empty graph / no inferred entry yields empty nodes/edges. Raises
    FileNotFoundError if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    import sylva  # lazy: the deterministic analysis lives in Rust (9.1 / 9.2)

    if root is None:
        primary = next(
            (e["symbol"] for e in sylva.infer_entrypoints(db_path) if e.get("primary")),
            None,
        )
        if primary is None:
            return {"root": None, "nodes": [], "edges": []}
        root = primary

    flow = flow_layout(db_path, root)  # reachable, BFS-layered from the root
    spine = sylva.main_spine(db_path, root)
    on_spine = {(n["symbol"], n["file"], n["line"]) for n in spine["nodes"]}

    nodes = [
        {**n, "on_spine": (n["name"], n["file"], n["line"]) in on_spine}
        for n in flow["nodes"]
    ]
    return {"root": root, "nodes": nodes, "edges": flow["edges"]}


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


def module_map(db_path, level="file"):
    """Feature 4.7 — a top-level architecture map: each file or package
    (directory) is a node, with cross-group call/import edges aggregated.

    Pure aggregation over the existing symbols + edges (no parsing). Grouping:
      - `level="file"`    — one node per file path (`files`=1), matching 4.6's
        `modules`.
      - `level="package"` — one node per directory (`os.path.dirname`), the
        natural package grain that collapses a large repo to a few dozen nodes;
        `files` counts the files grouped into it.

    Each node carries `{id, label, symbols, files}`. An edge A->B exists when any
    symbol in group A calls/imports one in group B; `weight` = the count of such
    cross-group symbol edges. Intra-group edges collapse away (no self-loops).

    Returns `{level, nodes, edges}`. Raises ValueError on an unknown level;
    FileNotFoundError if the database does not exist.
    """
    if level not in ("file", "package"):
        raise ValueError(f"unknown level: {level!r} (expected 'file' or 'package')")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        # One row per file, with its symbol count (files with no symbols included).
        file_rows = conn.execute(
            "SELECT f.id, f.path, COUNT(s.id) "
            "FROM files f LEFT JOIN symbols s ON s.file_id = f.id "
            "GROUP BY f.id"
        ).fetchall()
        # symbol id -> its file id, to map edges to groups.
        sym_file = conn.execute("SELECT id, file_id FROM symbols").fetchall()
        edges = conn.execute(
            "SELECT src_id, dst_id FROM edges WHERE kind IN ('calls', 'imports', 'inherits')"
        ).fetchall()
    finally:
        conn.close()

    def group_of_path(path):
        if level == "file":
            return path
        d = os.path.dirname(path)
        return d if d else "."  # top-level files share the '.' package

    # Group id -> {symbols, files}; and file id -> group id.
    groups = {}
    file_group = {}
    for fid, path, sym_count in file_rows:
        gid = group_of_path(path)
        file_group[fid] = gid
        g = groups.setdefault(gid, {"symbols": 0, "files": 0})
        g["symbols"] += sym_count
        g["files"] += 1

    sym_group = {sid: file_group.get(file_id) for sid, file_id in sym_file}

    weights = {}
    for src, dst in edges:
        sg, tg = sym_group.get(src), sym_group.get(dst)
        if sg is None or tg is None or sg == tg:
            continue  # dangling or intra-group — collapses away
        weights[(sg, tg)] = weights.get((sg, tg), 0) + 1

    def label_of(gid):
        parts = str(gid).replace("\\", "/").split("/")
        parts = [p for p in parts if p]
        return parts[-1] if parts else str(gid)

    nodes = [
        {"id": gid, "label": label_of(gid), "symbols": g["symbols"], "files": g["files"]}
        for gid, g in sorted(groups.items())
    ]
    map_edges = [
        {"source": s, "target": t, "weight": w}
        for (s, t), w in sorted(weights.items())
    ]
    return {"level": level, "nodes": nodes, "edges": map_edges}


def class_view(db_path):
    """Classes view: every class with the methods defined inside it, plus
    inheritance links between classes.

    A method is a `function` whose span nests inside the class (innermost class
    wins for nested classes). Returns `{classes: [{id, name, file, methods:
    [{id, name, coverage_state}]}], edges: [{source, target}]}` where edges are
    subclass -> base `inherits` relationships. Raises FileNotFoundError if the
    database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        cls_rows = conn.execute(
            "SELECT s.id, s.name, s.file_id, s.line_start, s.line_end, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.kind = 'class'"
        ).fetchall()
        funcs = conn.execute(
            "SELECT id, name, file_id, line_start, coverage_pct "
            "FROM symbols WHERE kind = 'function'"
        ).fetchall()
        inh = conn.execute(
            "SELECT src_id, dst_id FROM edges WHERE kind = 'inherits'").fetchall()
    finally:
        conn.close()

    # Assign each function to the innermost class (same file, smallest span
    # containing its start line).
    owner = {}
    for fid, _fn, ffid, fls, _cov in funcs:
        best = None
        for cid, _cn, cfid, cs, ce, _cp in cls_rows:
            if cfid == ffid and cs is not None and ce is not None and cs < (fls or 0) <= ce:
                span = ce - cs
                if best is None or span < best[1]:
                    best = (cid, span)
        if best:
            owner[fid] = best[0]

    meta = {fid: (name, cov) for fid, name, _f, _l, cov in funcs}
    methods_by_class = {}
    for fid, cid in owner.items():
        name, cov = meta[fid]
        methods_by_class.setdefault(cid, []).append(
            {"id": fid, "name": name, "coverage_state": coverage_state(cov)})

    classes = [
        {
            "id": cid, "name": cname, "file": cpath,
            "methods": sorted(methods_by_class.get(cid, []), key=lambda m: m["name"]),
        }
        for cid, cname, _cfid, _cs, _ce, cpath in cls_rows
    ]
    edges = [{"source": s, "target": t} for s, t in inh]
    return {"classes": sorted(classes, key=lambda c: c["name"]), "edges": edges}


def layer_view(db_path):
    """Microservice / architectural-layer view: symbols grouped by inferred
    layer (interface / business / data / transport) with cross-layer call edges.

    Returns `{archetype, layers: [{layer, symbols: [{id, name, file, endpoint}]}],
    edges: [{source, target}]}`, layers ordered interface -> business -> data ->
    transport. `endpoint` marks web-route entrypoints (interface). For a library
    (archetype != service) the layers collapse to `business` — the UI can note
    that the layered view is meant for services. Raises FileNotFoundError if the
    database does not exist.
    """
    import sylva

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    info = sylva.infer_layers(db_path)
    endpoints = {
        e["symbol"]
        for e in sylva.infer_entrypoints(db_path)
        if e.get("marker_kind") == "web_route"
    }
    layer_of = {(e["symbol"], e["file"]): e["layer"] for e in info["layers"]}

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.name, f.path FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE s.kind IN ('function', 'class')"
        ).fetchall()
        edges = conn.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'calls'").fetchall()
    finally:
        conn.close()

    ORDER = ["interface", "business", "data", "transport"]
    by_layer, id_layer = {}, {}
    for sid, name, path in rows:
        lyr = layer_of.get((name, path))
        if lyr is None:
            continue
        id_layer[sid] = lyr
        by_layer.setdefault(lyr, []).append(
            {"id": sid, "name": name, "file": path, "endpoint": name in endpoints})

    layers = [
        {"layer": l, "symbols": sorted(by_layer[l], key=lambda s: s["name"])}
        for l in ORDER if by_layer.get(l)
    ]
    cross = [{"source": s, "target": t} for s, t in edges if s in id_layer and t in id_layer]
    return {"archetype": info["archetype"], "layers": layers, "edges": cross}


def neighborhood(db_path, symbol, depth=1):
    """Feature 4.9 — the N-hop neighbourhood of `symbol`, for the focus view.

    The induced subgraph of every symbol within `depth` *undirected* hops of the
    centre over `calls`/`imports` edges — i.e. both callers and callees, unlike
    the directional flow (4.10) / blast radius (4.2). Each node carries its hop
    distance `dist` from the centre (0 = the centre itself). BFS with a visited
    set, so cycles terminate. `depth` is clamped at 0.

    Returns `{center, depth, nodes: [{id, name, kind, file, line, dist}],
    edges: [{source, target, kind}]}`. An unknown symbol yields empty
    nodes/edges. Raises FileNotFoundError if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    depth = max(0, int(depth))
    conn = sqlite3.connect(db_path)
    try:
        syms = conn.execute(
            "SELECT s.id, s.name, s.kind, s.line_start, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id"
        ).fetchall()
        edges = conn.execute(
            "SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('calls', 'imports', 'inherits')"
        ).fetchall()
    finally:
        conn.close()

    detail = {sid: (name, kind, line, path) for (sid, name, kind, line, path) in syms}
    # Undirected adjacency over calls/imports.
    adj = {}
    for src, dst, _kind in edges:
        adj.setdefault(src, set()).add(dst)
        adj.setdefault(dst, set()).add(src)

    centers = [sid for (sid, name, *_rest) in syms if name == symbol]
    if not centers:
        return {"center": symbol, "depth": depth, "nodes": [], "edges": []}

    # BFS out to `depth` hops; `dist` is the shortest hop count from any centre.
    dist = {c: 0 for c in centers}
    frontier = list(centers)
    for d in range(1, depth + 1):
        nxt = []
        for u in frontier:
            for v in adj.get(u, ()):  # only symbols that actually exist as nodes
                if v not in dist and v in detail:
                    dist[v] = d
                    nxt.append(v)
        frontier = nxt
        if not frontier:
            break

    reached = set(dist)
    nodes = [
        {
            "id": sid,
            "name": detail[sid][0],
            "kind": detail[sid][1],
            "file": detail[sid][3],
            "line": detail[sid][2],
            "dist": dist[sid],
        }
        for sid in dist
    ]
    nodes.sort(key=lambda n: (n["dist"], n["name"]))
    sub_edges = [
        {"source": s, "target": t, "kind": k}
        for (s, t, k) in edges
        if s in reached and t in reached
    ]
    return {"center": symbol, "depth": depth, "nodes": nodes, "edges": sub_edges}


def tests(db_path):
    """Feature 10.2 — tests that exercise real logic paths, for the sidebar
    "Logic paths" surface. Each test with `test_covers` edges → click to render
    its execution path (4.11) as a block diagram.

    Returns `[{test, file, covers}]` sorted by name. Raises FileNotFoundError if
    the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT s.name, f.path, COUNT(*) FROM edges e "
            "JOIN symbols s ON s.id = e.src_id JOIN files f ON f.id = s.file_id "
            "WHERE e.kind = 'test_covers' GROUP BY s.id ORDER BY s.name"
        ).fetchall()
    finally:
        conn.close()
    return [{"test": name, "file": path, "covers": n} for (name, path, n) in rows]


def entrypoints(db_path):
    """Feature 9.5 — the ranked inferred entrypoints (Feature 9.1) for the
    sidebar navigator, so the structural views are launchable in one click.

    Thin reuse of the Rust `infer_entrypoints`: a ranked list of
    `{symbol, file, line, reachable, is_marker, marker_kind, rank, primary}`.
    Raises FileNotFoundError if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    import sylva  # lazy: the inference lives in Rust (9.1)

    return sylva.infer_entrypoints(db_path)


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


def data_flow(db_path, symbol):
    """Feature 4.12 — the static parameter-flow subgraph reachable from `symbol`,
    as a layered flowchart. Reads the `dataflow` table (populated by Rust
    `build_dataflow`): an edge src->dst means src passes a parameter into dst.

    Starting from the symbol, BFS outbound over dataflow edges assigns each
    reached node a `layer` (hop depth), reusing the 4.10 layering. Edges carry
    the `param` that flows. Returns `{symbol, nodes: [{id, name, kind, file,
    line, layer}], edges: [{source, target, param}]}`. An unknown symbol (or one
    whose data flows nowhere) yields empty nodes/edges. Raises FileNotFoundError
    if the database does not exist.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        syms = conn.execute(
            "SELECT s.id, s.name, s.kind, s.line_start, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id"
        ).fetchall()
        flow_rows = conn.execute("SELECT src_id, dst_id, param FROM dataflow").fetchall()
    finally:
        conn.close()

    detail = {sid: (name, kind, line, path) for (sid, name, kind, line, path) in syms}
    name_ids = {}
    for sid, name, *_rest in syms:
        name_ids.setdefault(name, []).append(sid)
    adj = {}
    for src, dst, _param in flow_rows:
        adj.setdefault(src, []).append(dst)

    roots = name_ids.get(symbol, [])
    if not roots:
        return {"symbol": symbol, "nodes": [], "edges": []}

    layer = _bfs_layers(roots, adj)
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
    edges = [
        {"source": s, "target": t, "param": p}
        for (s, t, p) in flow_rows
        if s in reached and t in reached
    ]
    return {"symbol": symbol, "nodes": nodes, "edges": edges}


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

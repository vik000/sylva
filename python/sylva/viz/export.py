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
            "coverage": coverage,  # may be None; used by the 4.5 overlay later
            "degree": degree.get(sid, 0),
        }
        for (sid, name, kind, line, coverage, path) in symbols
    ]
    links = [{"source": src, "target": dst, "kind": kind} for (src, dst, kind) in edges]

    return {"nodes": nodes, "links": links}


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

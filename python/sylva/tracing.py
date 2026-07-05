"""Ingest runtime call traces (from `sylva.tracer`) into the graph, and serve
them as ordered execution flows.

The tracer records real caller→callee events per test, in order, keyed by
`(file, def-line, name)`. Here we resolve each endpoint to a graph symbol and
persist an ordered `call_trace` table (managed entirely in Python — no Rust /
schema migration). `trace_view` then returns the exact succession a test ran, so
the viz can render it as a top-down flowchart of boxes.
"""

import json
import os
import sqlite3


# --- symbol resolution (file suffix + span containment) -------------------- #

def _components(path):
    return [c for c in path.replace("\\", "/").split("/") if c and c != "."]


def _suffix_match(graph_parts, want_parts):
    if len(graph_parts) < len(want_parts):
        return False
    return graph_parts[-len(want_parts):] == want_parts


class _Resolver:
    """Resolve a runtime `(abs_file, def_line)` to a graph symbol id."""

    def __init__(self, conn):
        self._files = [(fid, path, _components(path))
                       for fid, path in conn.execute("SELECT id, path FROM files")]
        self._syms = {}
        for sid, fid, ls, le in conn.execute(
                "SELECT id, file_id, line_start, line_end FROM symbols "
                "WHERE kind IN ('function', 'class')"):
            self._syms.setdefault(fid, []).append((sid, ls, le))
        self._file_cache = {}

    def _file_id(self, abs_file):
        if abs_file in self._file_cache:
            return self._file_cache[abs_file]
        want = _components(abs_file)
        best = None
        for fid, _path, parts in self._files:
            # graph path is usually a suffix of the absolute runtime path
            if _suffix_match(want, parts) or _suffix_match(parts, want):
                if best is None or len(parts) > best[1]:
                    best = (fid, len(parts))
        fid = best[0] if best else None
        self._file_cache[abs_file] = fid
        return fid

    def resolve(self, abs_file, line):
        if not abs_file:
            return None
        fid = self._file_id(abs_file)
        if fid is None:
            return None
        best = None  # smallest span containing `line`
        for sid, ls, le in self._syms.get(fid, ()):
            if ls is None:
                continue
            end = le if le is not None else ls
            if ls <= line <= end:
                span = end - ls
                if best is None or span < best[1]:
                    best = (sid, span)
        return best[0] if best else None


def _ensure_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS call_trace ("
        " test TEXT NOT NULL, seq INTEGER NOT NULL, depth INTEGER NOT NULL,"
        " src_id INTEGER, dst_id INTEGER NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_trace_test ON call_trace(test)")


def ingest_trace(db_path, trace_file):
    """Read a tracer JSON file and persist ordered `call_trace` rows.

    Returns `{"tests": n, "calls": m}`. Re-ingesting a test replaces its rows
    (idempotent). Raises FileNotFoundError if the db or trace file is missing.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    if not os.path.exists(trace_file):
        raise FileNotFoundError(f"trace file not found: {trace_file}")

    with open(trace_file) as f:
        traces = json.load(f)

    conn = sqlite3.connect(db_path)
    try:
        _ensure_table(conn)
        res = _Resolver(conn)
        tests, calls = 0, 0
        for test, events in traces.items():
            rows = []
            for seq, depth, cf, cl, _cn, kf, kl, _kn in events:
                dst = res.resolve(kf, kl)
                if dst is None:
                    continue  # callee not a graph symbol (e.g. a dunder helper)
                src = res.resolve(cf, cl) if cf else None
                rows.append((test, seq, depth, src, dst))
            if not rows:
                continue
            conn.execute("DELETE FROM call_trace WHERE test = ?", (test,))
            conn.executemany(
                "INSERT INTO call_trace (test, seq, depth, src_id, dst_id) "
                "VALUES (?, ?, ?, ?, ?)", rows)
            tests += 1
            calls += len(rows)
        conn.commit()
        return {"tests": tests, "calls": calls}
    finally:
        conn.close()


# --- views ----------------------------------------------------------------- #

def _has_table(conn):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='call_trace'"
    ).fetchone() is not None


def list_traces(db_path):
    """Tests that have a recorded runtime trace, with their call counts."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        if not _has_table(conn):
            return []
        return [
            {"test": t, "calls": n}
            for t, n in conn.execute(
                "SELECT test, COUNT(*) FROM call_trace GROUP BY test ORDER BY test")
        ]
    finally:
        conn.close()


def trace_view(db_path, test):
    """The ordered execution flow for one test: nodes (with first-seen order +
    call depth) and directed edges in the real call sequence.

    Shape: `{test, nodes: [{id, name, kind, file, order, depth}], edges:
    [{source, target, seq}]}`. Unknown/untraced test → empty nodes/edges.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        if not _has_table(conn):
            return {"test": test, "nodes": [], "edges": []}
        rows = conn.execute(
            "SELECT seq, depth, src_id, dst_id FROM call_trace "
            "WHERE test = ? ORDER BY seq", (test,)).fetchall()
        if not rows:
            return {"test": test, "nodes": [], "edges": []}

        meta = {}
        for sid, name, kind, path in conn.execute(
                "SELECT s.id, s.name, s.kind, f.path FROM symbols s "
                "JOIN files f ON s.file_id = f.id"):
            meta[sid] = (name, kind, path)

        nodes, edges = {}, []
        for seq, depth, src, dst in rows:
            for sid, d in ((src, max(0, depth - 1)), (dst, depth)):
                if sid is None or sid not in meta:
                    continue
                if sid not in nodes:
                    name, kind, path = meta[sid]
                    nodes[sid] = {"id": sid, "name": name, "kind": kind,
                                  "file": path, "order": seq, "depth": d}
                else:
                    nodes[sid]["depth"] = min(nodes[sid]["depth"], d)
            if src is not None and src in meta and dst in meta:
                edges.append({"source": src, "target": dst, "seq": seq})
        return {"test": test,
                "nodes": sorted(nodes.values(), key=lambda n: n["order"]),
                "edges": edges}
    finally:
        conn.close()

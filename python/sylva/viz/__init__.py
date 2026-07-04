"""Sylva visualisation — a local, dependency-free interactive graph UI.

This is Sylva's first Python-native module (no Rust / PyO3): it reads the graph
database directly via stdlib `sqlite3`, exports a viz-shaped `graph.json`, and
serves a self-contained vanilla-Canvas force-directed diagram over the stdlib
HTTP server. See `export.build_graph` and `server.serve`.
"""

from .export import (
    architecture,
    build_graph,
    coverage_state,
    exec_path,
    export_graph_json,
    flow_layout,
    graph_version,
    neighborhood,
)
from .server import make_server, serve

__all__ = [
    "architecture",
    "build_graph",
    "coverage_state",
    "exec_path",
    "export_graph_json",
    "flow_layout",
    "graph_version",
    "neighborhood",
    "make_server",
    "serve",
]

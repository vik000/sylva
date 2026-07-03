"""Feature 4.1 — Call chain tracing.

`sylva.trace_calls(db_path, symbol, direction, depth)` walks `calls` edges
(built by 4.0). Each result: {name, kind, file, line, depth, direction}, where
depth 0 is the start symbol (direction 'self').
"""

import pytest

import sylva

# Call graph:  a -> b -> c -> d,  and  a -> d directly.
CHAIN = (
    "def d():\n    return 0\n\n"
    "def c():\n    return d()\n\n"
    "def b():\n    return c()\n\n"
    "def a():\n    b()\n    return d()\n"
)


def _graph(tmp_path, name, text):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    f = tmp_path / name
    f.write_text(text)
    sylva.write_symbols(str(db), str(f), sylva.extract_symbols(str(f)))
    sylva.build_edges(str(db))
    return db


def _names_at(result, depth):
    return {r["name"] for r in result if r["depth"] == depth}


def _by_name(result):
    return {r["name"]: r for r in result}


class TestGeneral:
    def test_direct_callers_inbound(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        result = sylva.trace_calls(str(db), "d", "inbound", 1)
        # Direct callers of d: c and a.
        assert _names_at(result, 1) == {"c", "a"}

    def test_multi_hop_outbound(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        result = sylva.trace_calls(str(db), "a", "outbound", 3)
        names = {r["name"] for r in result}
        assert names == {"a", "b", "c", "d"}
        assert _by_name(result)["a"]["depth"] == 0
        assert _by_name(result)["c"]["depth"] == 2  # a->b->c

    def test_both_directions_labeled(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        result = sylva.trace_calls(str(db), "c", "both", 1)
        by = {(r["name"], r["direction"]) for r in result}
        assert ("c", "self") in by       # start
        assert ("d", "outbound") in by   # c calls d
        assert ("b", "inbound") in by    # b calls c

    def test_result_shape(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        result = sylva.trace_calls(str(db), "a", "outbound", 1)
        assert set(result[0].keys()) == {"name", "kind", "file", "line", "depth", "direction"}


class TestEdge:
    def test_depth_zero_returns_only_self(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        result = sylva.trace_calls(str(db), "a", "outbound", 0)
        assert len(result) == 1
        assert result[0]["name"] == "a"
        assert result[0]["depth"] == 0
        assert result[0]["direction"] == "self"

    def test_depth_limit_is_strict(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        result = sylva.trace_calls(str(db), "a", "outbound", 1)
        names = {r["name"] for r in result}
        assert names == {"a", "b", "d"}  # c (depth 2) excluded
        assert "c" not in names

    def test_self_recursion_terminates(self, tmp_path):
        db = _graph(tmp_path, "r.py", "def r():\n    return r()\n")
        result = sylva.trace_calls(str(db), "r", "outbound", 10)
        # Self-edge must not loop; r appears once as the start.
        assert [r["name"] for r in result] == ["r"]

    def test_mutual_recursion_terminates(self, tmp_path):
        db = _graph(
            tmp_path, "cyc.py",
            "def x():\n    return y()\n\ndef y():\n    return x()\n",
        )
        result = sylva.trace_calls(str(db), "x", "outbound", 10)
        names = {r["name"] for r in result}
        assert names == {"x", "y"}  # terminates, no infinite loop
        assert _by_name(result)["y"]["depth"] == 1


class TestNegative:
    def test_unknown_symbol_returns_empty(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        assert sylva.trace_calls(str(db), "ghost", "outbound", 3) == []

    def test_invalid_direction_raises(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        with pytest.raises(ValueError):
            sylva.trace_calls(str(db), "a", "sideways", 3)

    def test_direction_validated_before_symbol(self, tmp_path):
        # A bad direction raises even when the symbol is unknown.
        db = _graph(tmp_path, "m.py", CHAIN)
        with pytest.raises(ValueError):
            sylva.trace_calls(str(db), "ghost", "sideways", 3)

    def test_negative_depth_raises(self, tmp_path):
        db = _graph(tmp_path, "m.py", CHAIN)
        with pytest.raises(ValueError):
            sylva.trace_calls(str(db), "a", "outbound", -1)

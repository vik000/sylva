"""Feature 5.6 — TypeScript/JavaScript call-edge resolution (issue #71).

`build_edges` now extracts TS/JS call sites (via the TSX grammar) and resolves
them with the existing language-neutral resolver (same-file, unique-global,
ambiguous-skip). `this.method()` resolves to the enclosing class's method for
free (7.10 class membership). Python behaviour is unchanged; `.rs` stays
black-boxed (no internal edges).
"""

import sqlite3

import pytest

import sylva


def _init(tmp_path):
    db = tmp_path / "sylva.db"
    sylva.init_db(str(db))
    return db


def _index(db, path, text):
    path.write_text(text)
    sylva.write_symbols(str(db), str(path), sylva.extract_symbols(str(path)))
    return path


def _call_edges(db):
    conn = sqlite3.connect(str(db))
    try:
        return {
            (s, d)
            for (s, d) in conn.execute(
                "SELECT s.name, d.name FROM edges e "
                "JOIN symbols s ON s.id = e.src_id JOIN symbols d ON d.id = e.dst_id "
                "WHERE e.kind = 'calls'"
            )
        }
    finally:
        conn.close()


def _dst_lines(db, src_name, dst_name):
    conn = sqlite3.connect(str(db))
    try:
        return sorted(
            r[0]
            for r in conn.execute(
                "SELECT d.line_start FROM edges e "
                "JOIN symbols s ON s.id = e.src_id JOIN symbols d ON d.id = e.dst_id "
                "WHERE e.kind = 'calls' AND s.name = ? AND d.name = ?",
                (src_name, dst_name),
            )
        )
    finally:
        conn.close()


class TestTsEdges:
    def test_simple_call(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.ts",
            "function b() {\n    return 1;\n}\n\nfunction a() {\n    return b();\n}\n",
        )
        sylva.build_edges(str(db))
        assert ("a", "b") in _call_edges(db)

    def test_arrow_calls(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.ts",
            "const helper = () => 1;\n\nfunction run() {\n    return helper();\n}\n",
        )
        sylva.build_edges(str(db))
        assert ("run", "helper") in _call_edges(db)

    def test_this_method_resolves_to_enclosing_class(self, tmp_path):
        db = _init(tmp_path)
        # Two classes define `work`; A.go() calls this.work() -> must be A.work.
        src = (
            "class A {\n"
            "    work() {\n        return 1;\n    }\n"          # line 2
            "    go() {\n        return this.work();\n    }\n"
            "}\n\n"
            "class B {\n"
            "    work() {\n        return 2;\n    }\n"          # line 10
            "}\n"
        )
        _index(db, tmp_path / "m.ts", src)
        sylva.build_edges(str(db))
        assert _dst_lines(db, "go", "work") == [2]  # A.work, not B.work

    def test_ambiguous_skipped(self, tmp_path):
        db = _init(tmp_path)
        # `dup` defined in two files; an unqualified call is ambiguous -> no edge.
        _index(db, tmp_path / "one.ts", "function dup() {}\n")
        _index(db, tmp_path / "two.ts", "function dup() {}\n")
        _index(db, tmp_path / "caller.ts", "function c() {\n    return dup();\n}\n")
        sylva.build_edges(str(db))
        assert not any(s == "c" and d == "dup" for (s, d) in _call_edges(db))


class TestJavaScript:
    def test_js_same_file(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.js",
            "function inner() { return 1; }\nfunction outer() { return inner(); }\n",
        )
        sylva.build_edges(str(db))
        assert ("outer", "inner") in _call_edges(db)


class TestRegressionAndScope:
    def test_python_unchanged(self, tmp_path):
        db = _init(tmp_path)
        _index(
            db,
            tmp_path / "m.py",
            "def helper():\n    return 1\n\ndef main():\n    return helper()\n",
        )
        sylva.build_edges(str(db))
        assert ("main", "helper") in _call_edges(db)

    def test_python_self_method_still_type_aware(self, tmp_path):
        db = _init(tmp_path)
        src = (
            "class A:\n"
            "    def run(self):\n        return 1\n\n"          # line 2
            "    def go(self):\n        return self.run()\n\n"
            "class B:\n"
            "    def run(self):\n        return 2\n"            # line 8
        )
        _index(db, tmp_path / "m.py", src)
        sylva.build_edges(str(db))
        assert _dst_lines(db, "go", "run") == [2]  # 7.10 still works

    def test_rust_no_internal_edges(self, tmp_path):
        db = _init(tmp_path)
        # .rs is black-boxed (foreign exports), so its internal calls form no edges.
        rs = tmp_path / "native.rs"
        rs.write_text(
            "#[pyfunction]\nfn a() { b(); }\n\nfn b() {}\n\n#[pymodule]\n"
            "fn native(m: &Bound<'_, PyModule>) -> PyResult<()> { Ok(()) }\n"
        )
        sylva.write_symbols(str(db), str(rs), sylva.extract_foreign_exports(str(rs)))
        sylva.build_edges(str(db))
        assert not any(s == "a" for (s, d) in _call_edges(db))  # no Rust-internal edges

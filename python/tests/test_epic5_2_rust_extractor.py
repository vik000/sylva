"""Feature 5.2 — Rust extractor (issue #21).

`extract_symbols('.rs')` parses Rust (tree-sitter-rust) behind the 5.1 trait,
extracting fns (incl. impl methods), structs, enums, and traits with the shared
symbol shape. Docstrings come from `///` doc comments. Parse errors return
partial results + the error sentinel. (`.rs` stays black-boxed in the analyze
pipeline — Feature 5.0 — so this is the extractor API.)
"""

import pytest

import sylva


RUST = (
    "/// A point in space.\n"
    "#[derive(Debug)]\n"
    "struct Point {\n    x: i64,\n    y: i64,\n}\n\n"
    "enum Shape {\n    Circle,\n    Square,\n}\n\n"
    "trait Draw {\n    fn draw(&self);\n}\n\n"
    "impl Point {\n"
    "    /// Construct a point.\n"
    "    fn new(x: i64, y: i64) -> Self {\n        Point { x, y }\n    }\n"
    "}\n\n"
    "fn free_function() -> i64 {\n    42\n}\n"
)


def _write(tmp_path, name, text, binary=False):
    p = tmp_path / name
    p.write_bytes(text) if binary else p.write_text(text)
    return str(p)


class TestExtract:
    def test_extracts_fn_struct_enum_trait(self, tmp_path):
        syms = sylva.extract_symbols(_write(tmp_path, "m.rs", RUST))
        kinds = {(s["name"], s["kind"]) for s in syms}
        assert ("Point", "struct") in kinds
        assert ("Shape", "enum") in kinds
        assert ("Draw", "trait") in kinds
        assert ("free_function", "function") in kinds

    def test_impl_methods_are_functions(self, tmp_path):
        syms = sylva.extract_symbols(_write(tmp_path, "m.rs", RUST))
        # `new` (inside `impl Point`) and `draw` (in the trait) are functions.
        names = {(s["name"], s["kind"]) for s in syms if s["kind"] == "function"}
        assert ("new", "function") in names
        assert ("draw", "function") in names

    def test_doc_comments_as_docstring(self, tmp_path):
        syms = {s["name"]: s for s in sylva.extract_symbols(_write(tmp_path, "m.rs", RUST))}
        assert syms["Point"]["docstring"] == "A point in space."   # /// above, past #[derive]
        assert syms["new"]["docstring"] == "Construct a point."

    def test_shared_symbol_shape(self, tmp_path):
        s = sylva.extract_symbols(_write(tmp_path, "m.rs", RUST))[0]
        assert set(s) == {
            "name", "kind", "line", "line_end", "docstring", "import_module", "import_name",
        }

    def test_line_spans(self, tmp_path):
        syms = {s["name"]: s for s in sylva.extract_symbols(_write(tmp_path, "m.rs", RUST))}
        # struct Point spans its multi-line body.
        assert syms["Point"]["line_end"] > syms["Point"]["line"]

    def test_in_list_languages(self):
        assert "rust" in sylva.list_languages()


class TestEdges:
    def test_empty_file(self, tmp_path):
        assert sylva.extract_symbols(_write(tmp_path, "empty.rs", "")) == []

    def test_comments_only(self, tmp_path):
        assert sylva.extract_symbols(_write(tmp_path, "c.rs", "// just a comment\n")) == []

    def test_nested_impls(self, tmp_path):
        src = (
            "mod outer {\n"
            "    struct Inner;\n"
            "    impl Inner {\n        fn method(&self) {}\n    }\n"
            "}\n"
        )
        syms = {(s["name"], s["kind"]) for s in sylva.extract_symbols(_write(tmp_path, "n.rs", src))}
        assert ("Inner", "struct") in syms
        assert ("method", "function") in syms  # nested impl method captured


class TestRobustness:
    def test_parse_error_returns_sentinel(self, tmp_path):
        # Valid item + broken syntax → partial results + error sentinel, no crash.
        src = "fn ok() -> i64 { 1 }\n\nfn broken( {\n"
        syms = sylva.extract_symbols(_write(tmp_path, "bad.rs", src))
        assert any(s["kind"] == "error" for s in syms)
        assert any(s["name"] == "ok" for s in syms)

    def test_binary_rust_file_raises(self, tmp_path):
        path = _write(tmp_path, "bin.rs", b"\x00\x01\xff\xfe", binary=True)
        with pytest.raises(ValueError):
            sylva.extract_symbols(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.extract_symbols(str(tmp_path / "nope.rs"))

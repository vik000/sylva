"""Feature 5.3 — TypeScript/JavaScript extractor (issue #22).

`extract_symbols` handles `.ts`/`.tsx`/`.js`/`.jsx` via tree-sitter-typescript
(TSX grammar, a JS/TS/JSX superset) behind the 5.1 trait — extracting functions
(declarations, methods, arrow assignments), classes, interfaces, and enums with
the shared symbol shape and JSDoc docstrings.
"""

import pytest

import sylva


TS = (
    "/** Greets a person. */\n"
    "export function greet(name: string): string {\n    return 'hi ' + name;\n}\n\n"
    "export const add = (a: number, b: number) => a + b;\n\n"
    "interface Shape {\n    area(): number;\n}\n\n"
    "class Circle implements Shape {\n"
    "    constructor(public r: number) {}\n"
    "    area(): number {\n        return 3.14 * this.r * this.r;\n    }\n"
    "}\n\n"
    "enum Color {\n    Red,\n    Green,\n}\n"
)


def _write(tmp_path, name, text, binary=False):
    p = tmp_path / name
    p.write_bytes(text) if binary else p.write_text(text)
    return str(p)


class TestExtract:
    def test_function_class_interface_enum(self, tmp_path):
        kinds = {(s["name"], s["kind"]) for s in sylva.extract_symbols(_write(tmp_path, "m.ts", TS))}
        assert ("greet", "function") in kinds        # declaration
        assert ("add", "function") in kinds          # arrow assignment
        assert ("Shape", "interface") in kinds
        assert ("Circle", "class") in kinds
        assert ("area", "function") in kinds         # method
        assert ("Color", "enum") in kinds

    def test_jsdoc_docstring(self, tmp_path):
        syms = {s["name"]: s for s in sylva.extract_symbols(_write(tmp_path, "m.ts", TS))}
        assert syms["greet"]["docstring"] == "Greets a person."  # /** … */ above `export`

    def test_shared_symbol_shape(self, tmp_path):
        s = sylva.extract_symbols(_write(tmp_path, "m.ts", TS))[0]
        assert set(s) == {
            "name", "kind", "line", "line_end", "docstring", "import_module", "import_name",
        }

    def test_languages_registered(self):
        langs = sylva.list_languages()
        assert "typescript" in langs and "javascript" in langs


class TestVariants:
    def test_tsx_with_jsx(self, tmp_path):
        src = (
            "export function Button() {\n"
            "    return <button>Click</button>;\n"
            "}\n"
        )
        names = {s["name"] for s in sylva.extract_symbols(_write(tmp_path, "c.tsx", src))}
        assert "Button" in names

    def test_plain_js_esm(self, tmp_path):
        src = "export function esm() { return 1; }\nexport const arrow = () => 2;\n"
        kinds = {(s["name"], s["kind"]) for s in sylva.extract_symbols(_write(tmp_path, "m.js", src))}
        assert ("esm", "function") in kinds and ("arrow", "function") in kinds

    def test_commonjs(self, tmp_path):
        src = "function cjs() { return 1; }\nmodule.exports = { cjs };\n"
        assert any(s["name"] == "cjs" for s in sylva.extract_symbols(_write(tmp_path, "m.js", src)))

    def test_jsx_extension(self, tmp_path):
        src = "function App() {\n    return <div/>;\n}\n"
        assert any(s["name"] == "App" for s in sylva.extract_symbols(_write(tmp_path, "a.jsx", src)))


class TestEdges:
    def test_empty_file(self, tmp_path):
        assert sylva.extract_symbols(_write(tmp_path, "empty.ts", "")) == []

    def test_comments_only(self, tmp_path):
        assert sylva.extract_symbols(_write(tmp_path, "c.ts", "// nothing here\n")) == []

    def test_parse_error_returns_sentinel(self, tmp_path):
        src = "function ok() { return 1; }\n\nfunction broken( {\n"
        syms = sylva.extract_symbols(_write(tmp_path, "bad.ts", src))
        assert any(s["kind"] == "error" for s in syms)
        assert any(s["name"] == "ok" for s in syms)

    def test_binary_ts_raises(self, tmp_path):
        path = _write(tmp_path, "bin.ts", b"\x00\xff\x01", binary=True)
        with pytest.raises(ValueError):
            sylva.extract_symbols(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sylva.extract_symbols(str(tmp_path / "nope.ts"))

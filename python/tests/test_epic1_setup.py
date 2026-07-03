import sylva


class TestImport:
    def test_module_importable(self):
        assert sylva is not None

    def test_module_name(self):
        assert sylva.__name__ == "sylva"

    def test_module_is_compiled_extension(self):
        # Guards the project's central premise: symbols must come from the
        # compiled Rust/PyO3 extension, not a stray pure-Python module
        # shadowing it. In this mixed layout the top-level `sylva` package
        # re-exports from the compiled `sylva.sylva` submodule; assert that
        # submodule is a native extension. If this regresses, every downstream
        # feature test would pass against a fake module.
        import importlib

        ext = importlib.import_module("sylva.sylva")
        assert ext.__file__.endswith((".so", ".pyd"))

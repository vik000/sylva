import sylva


class TestImport:
    def test_module_importable(self):
        assert sylva is not None

    def test_module_name(self):
        assert sylva.__name__ == "sylva"

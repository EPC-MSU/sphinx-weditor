from sphinx_weditor.sphinx_weditor import extract_module_name_by_referer


def test_simple():
    assert True


def test_extract_module():
    referer = 'http://localhost/_viewer/module/path/to/rst'
    assert extract_module_name_by_referer(referer) == 'module'


def test_extract_module_non_module():
    referer = 'http://localhost/_viewer/foo.html'
    assert extract_module_name_by_referer(referer) is None


def test_extract_module_root():
    referer = 'http://localhost/_viewer/module/index.html'
    assert extract_module_name_by_referer(referer) == 'module'

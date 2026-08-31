"""
Tests for config-file discovery.

Ported from ``readthedocs/config/tests/test_find.py``.
"""

import os

from conftest import apply_fs

from builder.config.config import CONFIG_FILENAME_REGEX
from builder.config.find import find_one


def test_find_no_files(tmpdir):
    with tmpdir.as_cwd():
        path = find_one(os.getcwd(), r"readthedocs.yml")
    assert path == ""


def test_find_at_root(tmpdir):
    apply_fs(tmpdir, {"readthedocs.yml": "", "otherfile.txt": ""})
    base = str(tmpdir)
    path = find_one(base, r"readthedocs\.yml")
    assert path == os.path.abspath(os.path.join(base, "readthedocs.yml"))


def test_find_does_not_search_subdirectories(tmpdir):
    apply_fs(tmpdir, {"subdir": {"readthedocs.yml": ""}})
    assert find_one(str(tmpdir), CONFIG_FILENAME_REGEX) == ""


def test_find_matches_all_supported_filenames(tmpdir):
    # The regex backs both the dotted and undotted spellings, .yml and .yaml.
    for filename in (
        ".readthedocs.yaml",
        ".readthedocs.yml",
        "readthedocs.yaml",
        "readthedocs.yml",
    ):
        directory = tmpdir.mkdir(filename.replace(".", "_"))
        apply_fs(directory, {filename: ""})
        found = find_one(str(directory), CONFIG_FILENAME_REGEX)
        assert found == os.path.join(str(directory), filename)

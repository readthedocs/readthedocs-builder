"""
Golden-fixture tests for the ported search/manifest parser.

The ``fixtures/search`` in/out pairs are copied verbatim from
``readthedocs/search/tests/data`` in readthedocs.org, where the same tests run
against the original parser. Committing them to both repositories is what
catches drift between the two: a parser change that alters the extracted
sections or the content hashes fails here until the port and the fixtures are
updated together (and ``HASHER_VERSION`` is bumped when the hashes change
meaning).
"""

import json
from pathlib import Path

import pytest

from builder.parsers import GenericParser

FIXTURES = Path(__file__).parent / "fixtures" / "search"


def parse(html_dir, page):
    parser = GenericParser(
        project_slug="test-project",
        version_slug="latest",
        html_path=str(html_dir),
    )
    return parser.parse(page)


@pytest.mark.parametrize(
    "html_dir, page, expected",
    [
        ("generic/in", "basic.html", "generic/out/basic.json"),
        ("sphinx/in", "page.html", "sphinx/out/page.json"),
        ("sphinx/in", "httpdomain.html", "sphinx/out/httpdomain.json"),
        ("sphinx/in", "no-title.html", "sphinx/out/no-title.json"),
        ("mkdocs/in/material", "index.html", "mkdocs/out/material.json"),
    ],
)
def test_parse_matches_readthedocs_golden_output(html_dir, page, expected):
    parsed = parse(FIXTURES / html_dir, page)

    expected_json = json.loads((FIXTURES / expected).read_text())
    # Some upstream golden files hold a list of pages, some a single page.
    if isinstance(expected_json, list):
        (expected_json,) = expected_json

    assert parsed == expected_json


def test_parse_missing_page_returns_empty_result():
    parsed = parse(FIXTURES / "generic/in", "does-not-exist.html")

    assert parsed == {
        "path": "does-not-exist.html",
        "title": "",
        "sections": [],
        "main_content_hash": None,
        "text_hash": None,
        "markup_hash": None,
    }

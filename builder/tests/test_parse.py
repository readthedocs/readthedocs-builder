"""Tests for the generated parse artifacts (manifest + search payload)."""

import gzip
import json

from builder.parse import HASHER_VERSION
from builder.parse import MANIFEST_FILE_NAME
from builder.parse import PARSER_VERSION
from builder.parse import SEARCH_PAYLOAD_FILE_NAME
from builder.parse import SEARCH_PAYLOAD_SCHEMA_VERSION
from builder.parse import generate_parse_artifacts


def _generate(tmp_path, pages):
    html_path = tmp_path / "html"
    html_path.mkdir()
    for path, content in pages.items():
        page = html_path / path
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(content)

    output_path = tmp_path / "diff"
    filenames = generate_parse_artifacts(
        project_slug="pip",
        version_slug="latest",
        build_id=42,
        html_path=str(html_path),
        output_path=str(output_path),
    )
    return output_path, filenames


def _read_payload(output_path):
    with gzip.open(output_path / SEARCH_PAYLOAD_FILE_NAME, "rt") as f:
        lines = [json.loads(line) for line in f]
    return lines[0], lines[1:]


PAGE = "<html><head><title>Test</title></head><body><h1 id='t'>Test</h1><p>Hello</p></body></html>"


def test_generates_both_artifacts(tmp_path):
    output_path, filenames = _generate(tmp_path, {"index.html": PAGE})

    assert filenames == [MANIFEST_FILE_NAME, SEARCH_PAYLOAD_FILE_NAME]
    assert (output_path / MANIFEST_FILE_NAME).exists()
    assert (output_path / SEARCH_PAYLOAD_FILE_NAME).exists()


def test_manifest_matches_server_format(tmp_path):
    # The structure ``readthedocs.filetreediff.dataclasses.FileTreeDiffManifest``
    # reads back with ``from_dict`` and writes with ``as_dict``.
    output_path, __ = _generate(
        tmp_path,
        {"index.html": PAGE, "api/index.html": PAGE, "skipped.txt": "not html"},
    )

    manifest = json.loads((output_path / MANIFEST_FILE_NAME).read_text())

    assert manifest["build"] == {"id": 42}
    assert manifest["hasher_version"] == HASHER_VERSION
    assert sorted(manifest["files"]) == ["api/index.html", "index.html"]
    entry = manifest["files"]["index.html"]
    assert entry["path"] == "index.html"
    for key in ("main_content_hash", "text_hash", "markup_hash"):
        assert len(entry[key]) == 32  # md5 hexdigest

    # Identical pages hash identically.
    assert entry["text_hash"] == manifest["files"]["api/index.html"]["text_hash"]


def test_payload_header_and_pages(tmp_path):
    output_path, __ = _generate(
        tmp_path,
        {"b.html": PAGE, "a.html": PAGE},
    )

    header, pages = _read_payload(output_path)

    assert header["schema_version"] == SEARCH_PAYLOAD_SCHEMA_VERSION
    assert header["parser_version"] == PARSER_VERSION
    assert header["hasher_version"] == HASHER_VERSION
    assert header["build_id"] == 42
    assert header["project"] == "pip"
    assert header["version"] == "latest"
    assert header["created"]

    # One line per page, sorted for deterministic output.
    assert [page["path"] for page in pages] == ["a.html", "b.html"]
    assert pages[0]["title"] == "Test"
    assert pages[0]["sections"] == [{"id": "t", "title": "Test", "content": "Hello"}]
    assert pages[0]["text_hash"]


def test_no_html_pages_writes_empty_artifacts(tmp_path):
    # An HTML dir without pages still produces a (valid, empty) manifest:
    # the server skips regenerating it and runs the side effects as usual.
    output_path, __ = _generate(tmp_path, {"only.txt": "no html here"})

    manifest = json.loads((output_path / MANIFEST_FILE_NAME).read_text())
    assert manifest["files"] == {}

    header, pages = _read_payload(output_path)
    assert header["build_id"] == 42
    assert pages == []

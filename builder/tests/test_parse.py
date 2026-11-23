"""Tests for the generated file tree diff manifest."""

import json
import os

from builder.parse import HASHER_VERSION
from builder.parse import MANIFEST_FILE_NAME
from builder.parse import generate_manifest


PAGE = "<html><head><title>Test</title></head><body><h1 id='t'>Test</h1><p>Hello</p></body></html>"


def _generate(tmp_path, pages):
    html_path = tmp_path / "html"
    html_path.mkdir()
    for path, content in pages.items():
        page = html_path / path
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(content)

    manifest_path = generate_manifest(
        project_slug="pip",
        version_slug="latest",
        build_id=42,
        html_path=str(html_path),
        output_path=str(tmp_path / "diff"),
    )
    return html_path, manifest_path


def test_manifest_matches_server_format(tmp_path):
    # The structure ``readthedocs.filetreediff.dataclasses.FileTreeDiffManifest``
    # reads back with ``from_dict`` and writes with ``as_dict``.
    __, manifest_path = _generate(
        tmp_path,
        {"index.html": PAGE, "api/index.html": PAGE, "skipped.txt": "not html"},
    )

    assert manifest_path == str(tmp_path / "diff" / MANIFEST_FILE_NAME)
    manifest = json.loads(open(manifest_path).read())

    assert manifest["build"] == {"id": 42}
    assert manifest["hasher_version"] == HASHER_VERSION
    assert sorted(manifest["files"]) == ["api/index.html", "index.html"]
    entry = manifest["files"]["index.html"]
    assert entry["path"] == "index.html"
    for key in ("main_content_hash", "text_hash", "markup_hash"):
        assert len(entry[key]) == 32  # md5 hexdigest

    # Identical pages hash identically.
    assert entry["text_hash"] == manifest["files"]["api/index.html"]["text_hash"]


def test_no_html_pages_writes_an_empty_manifest(tmp_path):
    # An HTML dir without pages still produces a (valid, empty) manifest:
    # the server skips regenerating it and runs the side effects as usual.
    __, manifest_path = _generate(tmp_path, {"only.txt": "no html here"})

    manifest = json.loads(open(manifest_path).read())
    assert manifest["files"] == {}
    assert manifest["build"] == {"id": 42}


def test_symlinks_are_never_followed(tmp_path):
    # The build can put anything in its output directory, including a link
    # to a file outside it. The parse runs outside the container, so it
    # must only ever read regular files.
    secret = tmp_path / "secret.html"
    secret.write_text(PAGE)
    html_path = tmp_path / "html"
    html_path.mkdir()
    (html_path / "index.html").write_text(PAGE)
    os.symlink(secret, html_path / "linked.html")
    os.symlink(tmp_path, html_path / "linked-dir")

    manifest_path = generate_manifest(
        project_slug="pip",
        version_slug="latest",
        build_id=42,
        html_path=str(html_path),
        output_path=str(tmp_path / "diff"),
    )

    manifest = json.loads(open(manifest_path).read())
    assert list(manifest["files"]) == ["index.html"]

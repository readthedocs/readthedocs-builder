"""
Post-build parsing: emit the file tree diff manifest.

Implements the builder side of the "parse docs on the builder" design
(``docs/dev/design/parse-on-the-builder.rst`` in readthedocs.org): every built
HTML page is parsed once, here, where the files are already on local disk,
and ``manifest.json`` ships with the build — byte-compatible with what
``readthedocs.projects.tasks.search.FileManifestIndexer`` writes server-side
(``readthedocs.filetreediff.dataclasses.FileTreeDiffManifest.as_dict``),
plus a ``hasher_version``.

The parse runs in the builder process, outside the build container, like the
upload step. The output directory is written by user code, so only regular
files are read: symlinks are skipped, never followed.

Failures here never fail the build: when the manifest is missing or belongs
to another build, the server regenerates it from storage in ``index_build``.
"""

import json
import os
import stat

import structlog

from builder.parsers import GenericParser


log = structlog.get_logger(__name__)

# File name under the version's ``diff/`` storage prefix. The prefix also
# holds the server-written ``base_manifest_snapshot.json`` for pull request
# versions, so the manifest is uploaded on its own — never a directory sync,
# which would delete it.
MANIFEST_FILE_NAME = "manifest.json"

# Version of the hashing that fills the manifest. Must match
# ``readthedocs.filetreediff.dataclasses.HASHER_VERSION``; bump both only when
# a change alters what the hashes mean (``get_diff`` marks a mismatch between
# manifests as outdated instead of reporting every file as modified).
HASHER_VERSION = 1


def generate_manifest(
    *,
    project_slug: str,
    version_slug: str,
    build_id: int,
    html_path: str,
    output_path: str,
) -> str:
    """
    Parse every HTML page under ``html_path`` and write the manifest.

    Returns the path of the manifest written into ``output_path``
    (created if missing), for the caller to upload.
    """
    parser = GenericParser(
        project_slug=project_slug,
        version_slug=version_slug,
        html_path=html_path,
    )

    files = {}
    for page in sorted(_walk_html_files(html_path)):
        processed = parser.parse(page)
        files[page] = {
            "path": page,
            "main_content_hash": processed["main_content_hash"],
            "text_hash": processed["text_hash"],
            "markup_hash": processed["markup_hash"],
        }

    manifest = {
        "files": files,
        "build": {"id": build_id},
        "hasher_version": HASHER_VERSION,
    }
    os.makedirs(output_path, exist_ok=True)
    manifest_path = os.path.join(output_path, MANIFEST_FILE_NAME)
    with open(manifest_path, "w", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file)

    log.info("Manifest generated.", pages=len(files))
    return manifest_path


def _walk_html_files(html_path: str):
    """
    Yield every ``.html`` regular file under ``html_path``, relative to it.

    Symlinks are skipped (``os.walk`` doesn't descend into linked directories
    either), so nothing outside the output directory is ever read.
    """
    for root, __, filenames in os.walk(html_path):
        for filename in filenames:
            if not filename.endswith(".html"):
                continue
            path = os.path.join(root, filename)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                log.warning("Skipping non-regular file.", path=path)
                continue
            yield os.path.relpath(path, html_path)

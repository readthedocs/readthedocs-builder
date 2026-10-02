"""
Post-build parsing: emit the file tree diff manifest and the search payload.

Implements the builder side of the "parse docs on the builder" design
(``docs/dev/design/parse-on-the-builder.rst`` in readthedocs.org): every built
HTML page is parsed once, here, where the files are already on local disk,
and the output ships with the build as two extra artifacts:

- ``manifest.json`` — the file tree diff manifest, byte-compatible with what
  ``readthedocs.projects.tasks.search.FileManifestIndexer`` writes server-side
  (``readthedocs.filetreediff.dataclasses.FileTreeDiffManifest.as_dict``).
- ``search.jsonl.gz`` — the search payload: a gzipped JSONL file whose first
  line is a metadata header and every following line one page's parser output.
  Server-side ingest reads the header first and must refuse schema majors it
  doesn't know (the tolerant-reader contract from the design doc).

Both are uploaded under the version's ``diff/`` storage prefix, which the
per-build scoped STS credentials already cover. Failures here never fail the
build: when the manifest is missing or belongs to another build, the server
falls back to regenerating everything from storage in ``index_build``.
"""

import gzip
import json
import os
from datetime import datetime
from datetime import timezone

import structlog

from builder.parsers import GenericParser


log = structlog.get_logger(__name__)

# File names under the version's ``diff/`` storage prefix. The prefix also
# holds the server-written ``base_manifest_snapshot.json`` for external
# versions, so these files are uploaded individually — never a directory sync,
# which would delete it.
MANIFEST_FILE_NAME = "manifest.json"
SEARCH_PAYLOAD_FILE_NAME = "search.jsonl.gz"

# Format of the search payload file itself. Gates ingest server-side: bump the
# major for breaking layout changes, the minor when adding fields a tolerant
# reader can ignore.
SEARCH_PAYLOAD_SCHEMA_VERSION = "1.0"

# Which extractor produced the content. Bump on any parser behavior change.
# Never gates ingest — it exists for observability and for targeting
# re-extracts of versions parsed by old extractors.
PARSER_VERSION = 1

# Version of the hashing that fills the manifest. Must match
# ``readthedocs.filetreediff.dataclasses.HASHER_VERSION``; bump both only when
# a change alters what the hashes mean (``get_diff`` marks a mismatch between
# manifests as outdated instead of reporting every file as modified).
HASHER_VERSION = 1


def generate_parse_artifacts(
    *,
    project_slug: str,
    version_slug: str,
    build_id: int,
    html_path: str,
    output_path: str,
) -> list[str]:
    """
    Parse every HTML page under ``html_path`` and write both artifacts.

    Returns the file names written into ``output_path`` (created if missing),
    for the caller to upload.
    """
    parser = GenericParser(
        project_slug=project_slug,
        version_slug=version_slug,
        html_path=html_path,
    )
    pages = sorted(_walk_html_files(html_path))

    header = {
        "schema_version": SEARCH_PAYLOAD_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "hasher_version": HASHER_VERSION,
        "build_id": build_id,
        "project": project_slug,
        "version": version_slug,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    manifest_files = {}

    os.makedirs(output_path, exist_ok=True)
    payload_path = os.path.join(output_path, SEARCH_PAYLOAD_FILE_NAME)
    with gzip.open(payload_path, "wt", encoding="utf-8") as payload_file:
        payload_file.write(json.dumps(header) + "\n")
        for page in pages:
            processed = parser.parse(page)
            payload_file.write(json.dumps(processed) + "\n")
            manifest_files[page] = {
                "path": page,
                "main_content_hash": processed["main_content_hash"],
                "text_hash": processed["text_hash"],
                "markup_hash": processed["markup_hash"],
            }

    manifest = {
        "files": manifest_files,
        "build": {"id": build_id},
        "hasher_version": HASHER_VERSION,
    }
    manifest_path = os.path.join(output_path, MANIFEST_FILE_NAME)
    with open(manifest_path, "w", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file)

    log.info(
        "Parse artifacts generated.",
        pages=len(pages),
        payload_size=os.path.getsize(payload_path),
    )
    return [MANIFEST_FILE_NAME, SEARCH_PAYLOAD_FILE_NAME]


def _walk_html_files(html_path: str):
    """Yield every ``.html`` file under ``html_path``, relative to it."""
    for root, __, filenames in os.walk(html_path):
        for filename in filenames:
            if filename.endswith(".html"):
                yield os.path.relpath(os.path.join(root, filename), html_path)

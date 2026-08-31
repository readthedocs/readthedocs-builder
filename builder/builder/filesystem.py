"""
Filesystem helpers with path-containment and symlink checks.

Ported from ``readthedocs.core.utils.filesystem``. Replaces
``django.core.exceptions.SuspiciousFileOperation`` with a plain
``PermissionError``; everything else is a faithful port. Used by:

- :class:`builder.director.BuildDirector` (``store_readthedocs_build_yaml``)
- :class:`builder.python_envs.Conda` (read/write user's environment.yml)
- :class:`builder.vcs.Backend` (replaces the inlined _safe_rmtree)

Every path they guard is inside the user's checkout,
which the user's own build commands can
write — including symlinks. The runner reads and writes that tree from
the *host*, so a symlink escaping the docroot resolves against the
instance's real filesystem rather than a disposable container's.

Written for GHSA-368m-86q9-m99w; keep them.
"""

import shutil
from pathlib import Path

import structlog

from builder import settings
from builder.exceptions import BuildUserError
from builder.exceptions import FileIsNotRegularFile
from builder.exceptions import SymlinkOutsideBasePath
from builder.exceptions import UnsupportedSymlinkFileError


log = structlog.get_logger(__name__)


MAX_FILE_SIZE_BYTES = 1024 * 1024 * 1024  # 1 GB


def assert_path_is_inside_docroot(path):
    """
    Assert that ``path`` (after resolving symlinks) lives under ``DOCROOT``.

    Raises :class:`PermissionError` (in lieu of upstream's Django
    ``SuspiciousFileOperation``) on traversal.

    .. warning::

       Not safe against TOCTOU attacks; the caller is responsible for
       preventing the underlying file from being mutated mid-operation.
    """
    resolved_path = Path(path).absolute().resolve()
    docroot = Path(settings.DOCROOT).absolute()
    if not resolved_path.is_relative_to(docroot):
        log.error(
            "Suspicious operation outside the docroot directory.",
            path_resolved=str(resolved_path),
            docroot=settings.DOCROOT,
        )
        raise PermissionError(f"Path {path} is outside DOCROOT {settings.DOCROOT}")


def safe_open(
    path,
    *args,
    allow_symlinks=False,
    base_path=None,
    max_size_bytes=MAX_FILE_SIZE_BYTES,
    **kwargs,
):
    """
    Open a file with symlink, traversal and size guards.

    See upstream's ``readthedocs.core.utils.filesystem.safe_open`` for the
    rationale (GHSA-368m-86q9-m99w). When ``allow_symlinks=True``,
    ``base_path`` is mandatory and the resolved target must live inside it.
    Files over ``max_size_bytes`` raise :class:`BuildUserError`.

    Extra ``*args`` and ``**kwargs`` are forwarded to ``Path.open``.
    """
    if allow_symlinks and not base_path:
        raise ValueError("base_path must be given if symlinks are allowed.")

    path = Path(path).absolute()
    structlog.contextvars.bind_contextvars(path_resolved=str(path.resolve()))

    if path.exists() and not path.is_file():
        raise FileIsNotRegularFile(FileIsNotRegularFile.SYMLINK_USED)

    if not allow_symlinks and path.is_symlink():
        log.info("Skipping file because it's a symlink.")
        raise UnsupportedSymlinkFileError(UnsupportedSymlinkFileError.SYMLINK_USED)

    resolved_path = path.resolve()

    if resolved_path.exists():
        file_size = resolved_path.stat().st_size
        if file_size > max_size_bytes:
            log.info("File is too large.", size_bytes=file_size)
            raise BuildUserError(BuildUserError.FILE_TOO_LARGE)

    if allow_symlinks and base_path:
        base_path = Path(base_path).absolute()
        if not resolved_path.is_relative_to(base_path):
            log.info("Path traversal via symlink.")
            raise SymlinkOutsideBasePath(SymlinkOutsideBasePath.SYMLINK_USED)

    assert_path_is_inside_docroot(resolved_path)

    # pylint: disable=unspecified-encoding
    return resolved_path.open(*args, **kwargs)


def safe_rmtree(path, *args, **kwargs):
    """
    rmtree wrapper that refuses to follow symlinks.

    Returns ``None`` if ``path`` is a symlink (logged); otherwise asserts the
    path is under DOCROOT and forwards to :func:`shutil.rmtree`.
    """
    path = Path(path)
    if path.is_symlink():
        log.info(
            "Not deleting directory because it's a symlink.",
            path=str(path),
            resolved_path=str(path.resolve()),
        )
        return None
    assert_path_is_inside_docroot(path)
    return shutil.rmtree(path, *args, **kwargs)

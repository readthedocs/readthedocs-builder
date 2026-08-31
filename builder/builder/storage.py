"""
Storage layer for the build runner.

Ported from ``readthedocs.projects.tasks.storage``. Replaces upstream's
django-storages-backed S3 wrapper with a thin :mod:`boto3` client that
exposes the methods the build pipeline actually consumes:

- :meth:`BuildToolsStorage.exists` / :meth:`BuildToolsStorage.open` —
  read-only access to the cached asdf tool-tarball, used by
  :meth:`builder.director.BuildDirector.install_build_tools` to skip the
  multi-minute compile.
- :meth:`BuildMediaStorage.rclone_sync_directory` /
  :meth:`BuildMediaStorage.delete_directory` — read/write access to the
  build-artifacts bucket.

Artifacts are uploaded with ``rclone sync``.

Storage clients are obtained via :func:`get_storage`, which mints
**per-build, scoped STS credentials** via
``POST /api/v2/build/<id>/credentials/storage``. The credentials are
scoped to the right bucket/prefix and expire with the build, so the
runner never holds long-lived AWS keys.
"""

import io
import os
import tarfile
from enum import StrEnum
from enum import auto
from functools import cached_property

import boto3
import botocore.config
import botocore.exceptions
import structlog

from builder.exceptions import BuildAppError
from builder.rclone import RCloneS3Remote
from builder.rclone import SuspiciousFileOperation


log = structlog.get_logger(__name__)


class StorageType(StrEnum):
    build_media = auto()
    build_tools = auto()
    build_uploads = auto()


def get_storage(*, build_id, api_client, storage_type: StorageType, endpoint_url=None):
    """
    Build a storage client scoped to ``build_id``.

    Hits the API to mint per-build STS credentials, then returns the right
    storage subclass for the given type. Any error fetching credentials is
    wrapped in :class:`BuildAppError` (matching upstream behaviour).
    ``endpoint_url`` overrides the AWS endpoint; development points it at an
    S3-compatible server instead.
    """
    try:
        credentials = api_client.build(f"{build_id}/credentials/storage").post(
            {"type": storage_type.value}
        )
    except Exception:
        raise BuildAppError(
            BuildAppError.GENERIC_WITH_BUILD_ID,
            exception_message="Error getting scoped credentials.",
        )

    s3 = credentials["s3"]
    cls = _STORAGE_BY_TYPE[storage_type]
    return cls(
        access_key=s3["access_key_id"],
        secret_key=s3["secret_access_key"],
        security_token=s3["session_token"],
        bucket_name=s3["bucket_name"],
        region_name=s3["region_name"],
        endpoint_url=endpoint_url,
    )


class _S3StorageBase:
    """Common boto3 S3 client + ``exists`` shared by both storage flavors."""

    def __init__(
        self,
        *,
        bucket_name,
        region_name,
        access_key=None,
        secret_key=None,
        security_token=None,
        endpoint_url=None,
    ):
        self._bucket = bucket_name
        # Kept for ``BuildMediaStorage._rclone``, which needs the same
        # credentials as the boto3 client below.
        self._access_key = access_key
        self._secret_key = secret_key
        self._security_token = security_token
        self._region_name = region_name
        self._endpoint_url = endpoint_url
        # Forward only the cred kwargs that were provided. In practice
        # :func:`get_storage` always passes the full STS triple, but keeping
        # these conditional avoids handing boto3 an explicit ``None`` (which
        # would disable its own credential resolution).
        client_kwargs = {"region_name": region_name}
        if access_key:
            client_kwargs["aws_access_key_id"] = access_key
        if secret_key:
            client_kwargs["aws_secret_access_key"] = secret_key
        if security_token:
            client_kwargs["aws_session_token"] = security_token
        if endpoint_url:
            # Self-hosted S3-compatible servers (rustfs, MinIO, LocalStack)
            # rarely support virtual-host-style addressing (which needs DNS
            # for ``<bucket>.<host>``); force path-style.
            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["config"] = botocore.config.Config(
                s3={"addressing_style": "path"},
            )
        self._client = boto3.client("s3", **client_kwargs)

    def exists(self, path: str) -> bool:
        """
        ``True`` if ``path`` exists in the bucket.

        S3 returns 403 (not 404) on HeadObject when the IAM policy doesn't
        grant ``s3:ListBucket`` — and our scoped per-build credentials
        deliberately don't, since they only need to read specific keys
        under a known prefix. We treat 403 the same as 404: the lookup
        can't see the object, so callers should fall back to the cache-miss
        path. (If a real "access denied" turns up later it will resurface
        via :meth:`open` / ``get_object`` against the bucket.)
        """
        try:
            self._client.head_object(Bucket=self._bucket, Key=path)
            return True
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound", "403", "Forbidden", "AccessDenied"):
                return False
            raise

    def open(self, path: str, mode: str = "rb"):
        """
        Return a file-like for ``path``. Only ``mode='rb'`` is supported.

        The upstream consumer feeds the result to :func:`tarfile.open` or :func:`io.BytesIO.read`,
        which requires either a seekable stream or one of tarfile's
        streaming modes. We download the object body into a :class:`BytesIO`
        so callers don't need to care; tool tarballs are well under a few
        hundred MB so the buffer is fine.
        """
        if mode != "rb":
            raise ValueError(f"Storage only supports mode='rb', got {mode!r}")
        resp = self._client.get_object(Bucket=self._bucket, Key=path)
        return io.BytesIO(resp["Body"].read())

    def download_to_path(self, path: str, destination):
        """
        Stream ``path`` into the local file ``destination``.

        Unlike :meth:`open`, this never holds the whole object in memory:
        boto3's managed transfer writes to the file as it downloads.
        """
        with open(destination, "wb") as fd:
            self._client.download_fileobj(Bucket=self._bucket, Key=path, Fileobj=fd)


class BuildUploadsStorage(_S3StorageBase):
    """Read-only access to the build-uploads bucket."""


class BuildToolsStorage(_S3StorageBase):
    """Read-only access to the asdf-tools tarball cache."""


class BuildMediaStorage(_S3StorageBase):
    """Read/write access to the build-artifacts bucket."""

    @cached_property
    def _rclone(self):
        """
        Ported from ``readthedocs.storage.s3_storage.RTDS3Storage._rclone``.

        The credentials are the per-build STS triple minted by
        :func:`get_storage`; rclone receives them as env vars (including
        ``RCLONE_S3_SESSION_TOKEN``) rather than argv, so they stay out of logs.

        The provider is derived from the endpoint rather than mirroring
        readthedocs.org's ``settings.S3_PROVIDER``: an endpoint means an
        S3-compatible server, which rclone calls ``Other``. Upstream forwards
        its own provider name (``rustfs``), which rclone doesn't recognise —
        it warns and falls back to exactly this generic behaviour anyway.
        """
        return RCloneS3Remote(
            bucket_name=self._bucket,
            access_key_id=self._access_key,
            secret_access_key=self._secret_key,
            session_token=self._security_token,
            region=self._region_name or "",
            endpoint=self._endpoint_url,
            provider="Other" if self._endpoint_url else "AWS",
        )

    def rclone_sync_directory(self, source, destination):
        """
        Sync a directory recursively to storage using rclone sync.

        Ported from ``readthedocs.storage.mixins.RTDBaseStorage``. Upstream's
        ``_check_suspicious_path`` (symlink + docroot containment) is dropped:
        it guards against a *shared* builder host where one project's build
        could reach another's checkout. Here every build gets its own container
        and its own STS credentials scoped to its own prefix, so the docroot
        check has nothing left to protect. The empty-destination guard is kept —
        it's cheap and catches a caller bug that would wipe the bucket.
        """
        if destination in ("", "/"):
            raise SuspiciousFileOperation("Syncing all storage cannot be right")

        return self._rclone.sync(source, destination)

    def delete_directory(self, remote_prefix: str):
        """Delete every object under ``remote_prefix`` (paginated, batched)."""
        prefix = remote_prefix.rstrip("/") + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": objects},
                )


def extract_tarball_to(remote_fd, extract_path: str):
    """
    Extract a gzipped tarball from ``remote_fd`` into ``extract_path``.

    Convenience wrapper around :mod:`tarfile`, used by the director's tool
    cache hit path. Creates ``extract_path`` if it doesn't exist.
    """
    os.makedirs(extract_path, exist_ok=True)
    with tarfile.open(fileobj=remote_fd) as tar:
        # ``filter="fully_trusted"`` is required: the build-tools cache tarballs
        # are RTD-produced and trusted, and miniforge/conda ones contain
        # absolute-path symlinks (e.g. ``miniforge3-*/_conda``). Python 3.14's
        # default ``data`` filter rejects those with ``AbsoluteLinkError``,
        # which would break every cache-hit conda/mamba tool install.
        tar.extractall(extract_path, filter="fully_trusted")


_STORAGE_BY_TYPE = {
    StorageType.build_tools: BuildToolsStorage,
    StorageType.build_media: BuildMediaStorage,
    StorageType.build_uploads: BuildUploadsStorage,
}

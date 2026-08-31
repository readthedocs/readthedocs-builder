"""
Tests for the S3 storage layer.

Ported from ``readthedocs.rtd_tests.tests.test_build_storage``. Differences,
all intentional:

- The builder storage is S3-only (no local backend), so ``boto3.client`` is
  stubbed rather than exercising a real bucket.
- Upstream's symlink / outside-docroot ``_check_suspicious_path`` guard was
  dropped (each build is isolated with its own scoped credentials), so those
  tests don't apply; only the empty-destination guard is ported.
"""

import gzip
import io
import tarfile
from unittest import mock

import botocore.exceptions
import pytest

from builder.exceptions import BuildAppError
from builder.rclone import SuspiciousFileOperation
from builder.storage import BuildMediaStorage
from builder.storage import BuildToolsStorage
from builder.storage import BuildUploadsStorage
from builder.storage import StorageType
from builder.storage import extract_tarball_to
from builder.storage import get_storage


def _client_error(code):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "x"}}, "HeadObject"
    )


def make_tools_storage(**overrides):
    kwargs = {"bucket_name": "tools", "region_name": "us-east-1"}
    kwargs.update(overrides)
    with mock.patch("builder.storage.boto3.client") as client:
        storage = BuildToolsStorage(**kwargs)
    return storage, client


def make_media_storage(**overrides):
    kwargs = {"bucket_name": "media", "region_name": "us-east-1"}
    kwargs.update(overrides)
    with mock.patch("builder.storage.boto3.client"):
        return BuildMediaStorage(**kwargs)


# ---------------------------------------------------------------------------
# boto3 client construction
# ---------------------------------------------------------------------------


def test_client_forwards_credentials():
    with mock.patch("builder.storage.boto3.client") as client:
        BuildToolsStorage(
            bucket_name="b",
            region_name="us-east-1",
            access_key="AKIA",
            secret_key="secret",
            security_token="token",
        )
    _, kwargs = client.call_args
    assert kwargs["aws_access_key_id"] == "AKIA"
    assert kwargs["aws_secret_access_key"] == "secret"
    assert kwargs["aws_session_token"] == "token"


def test_client_omits_missing_credentials():
    # Without creds, boto3's own resolution must not be disabled by explicit None.
    with mock.patch("builder.storage.boto3.client") as client:
        BuildToolsStorage(bucket_name="b", region_name="us-east-1")
    _, kwargs = client.call_args
    assert "aws_access_key_id" not in kwargs


def test_client_uses_path_style_for_custom_endpoint():
    with mock.patch("builder.storage.boto3.client") as client:
        BuildToolsStorage(
            bucket_name="b",
            region_name="us-east-1",
            endpoint_url="http://localhost:9000",
        )
    _, kwargs = client.call_args
    assert kwargs["endpoint_url"] == "http://localhost:9000"
    assert kwargs["config"].s3["addressing_style"] == "path"


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


def test_exists_true_when_head_succeeds():
    storage, _ = make_tools_storage()
    storage._client.head_object.return_value = {}
    assert storage.exists("some/key") is True


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound", "403", "Forbidden", "AccessDenied"])
def test_exists_false_on_missing_or_forbidden(code):
    storage, _ = make_tools_storage()
    storage._client.head_object.side_effect = _client_error(code)
    assert storage.exists("some/key") is False


def test_exists_reraises_unexpected_errors():
    storage, _ = make_tools_storage()
    storage._client.head_object.side_effect = _client_error("500")
    with pytest.raises(botocore.exceptions.ClientError):
        storage.exists("some/key")


# ---------------------------------------------------------------------------
# BuildToolsStorage.open
# ---------------------------------------------------------------------------


def test_open_returns_object_body_as_bytesio():
    storage, _ = make_tools_storage()
    storage._client.get_object.return_value = {"Body": io.BytesIO(b"tarball-bytes")}
    fd = storage.open("tools/python.tar.gz")
    assert fd.read() == b"tarball-bytes"


def test_open_rejects_non_binary_mode():
    storage, _ = make_tools_storage()
    with pytest.raises(ValueError):
        storage.open("tools/python.tar.gz", mode="r")


# ---------------------------------------------------------------------------
# BuildMediaStorage
# ---------------------------------------------------------------------------


def test_rclone_sync_delegates_to_rclone():
    storage = make_media_storage()
    # Pre-seed the cached_property so no real RCloneS3Remote is built.
    rclone = mock.MagicMock()
    storage.__dict__["_rclone"] = rclone
    storage.rclone_sync_directory("/local/html", "html/latest")
    rclone.sync.assert_called_once_with("/local/html", "html/latest")


@pytest.mark.parametrize("destination", ["", "/"])
def test_rclone_sync_refuses_to_wipe_the_bucket(destination):
    storage = make_media_storage()
    with pytest.raises(SuspiciousFileOperation):
        storage.rclone_sync_directory("/local/html", destination)


def test_rclone_property_uses_generic_provider_for_custom_endpoint():
    storage = make_media_storage(endpoint_url="http://localhost:9000")
    assert storage._rclone.env_vars["RCLONE_S3_PROVIDER"] == "Other"


def test_rclone_property_uses_aws_provider_without_endpoint():
    storage = make_media_storage()
    assert storage._rclone.env_vars["RCLONE_S3_PROVIDER"] == "AWS"


def test_delete_directory_batches_keys_per_page():
    storage = make_media_storage()
    paginator = mock.MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": "html/latest/a"}, {"Key": "html/latest/b"}]},
        {"Contents": [{"Key": "html/latest/c"}]},
    ]
    storage._client.get_paginator.return_value = paginator

    storage.delete_directory("html/latest")

    assert storage._client.delete_objects.call_count == 2
    first = storage._client.delete_objects.call_args_list[0][1]
    assert first["Delete"]["Objects"] == [{"Key": "html/latest/a"}, {"Key": "html/latest/b"}]


def test_delete_directory_skips_empty_pages():
    storage = make_media_storage()
    paginator = mock.MagicMock()
    paginator.paginate.return_value = [{}]  # no Contents
    storage._client.get_paginator.return_value = paginator

    storage.delete_directory("html/latest")
    storage._client.delete_objects.assert_not_called()


# ---------------------------------------------------------------------------
# get_storage
# ---------------------------------------------------------------------------


def _credentials_payload():
    return {
        "s3": {
            "access_key_id": "AKIA",
            "secret_access_key": "secret",
            "session_token": "token",
            "bucket_name": "media",
            "region_name": "us-east-1",
        }
    }


def test_get_storage_returns_the_right_subclass():
    api_client = mock.MagicMock()
    api_client.build().post.return_value = _credentials_payload()
    with mock.patch("builder.storage.boto3.client"):
        storage = get_storage(
            build_id=42, api_client=api_client, storage_type=StorageType.build_media
        )
    assert isinstance(storage, BuildMediaStorage)


def test_get_storage_tools_type_returns_tools_storage():
    api_client = mock.MagicMock()
    api_client.build().post.return_value = _credentials_payload()
    with mock.patch("builder.storage.boto3.client"):
        storage = get_storage(
            build_id=42, api_client=api_client, storage_type=StorageType.build_tools
        )
    assert isinstance(storage, BuildToolsStorage)


def test_get_storage_posts_the_storage_type():
    api_client = mock.MagicMock()
    api_client.build().post.return_value = _credentials_payload()
    with mock.patch("builder.storage.boto3.client"):
        get_storage(build_id=42, api_client=api_client, storage_type=StorageType.build_tools)
    api_client.build().post.assert_called_with({"type": "build_tools"})


def test_get_storage_wraps_credential_errors():
    api_client = mock.MagicMock()
    api_client.build().post.side_effect = Exception("boom")
    with pytest.raises(BuildAppError) as excinfo:
        get_storage(build_id=42, api_client=api_client, storage_type=StorageType.build_media)
    assert excinfo.value.message_id == BuildAppError.GENERIC_WITH_BUILD_ID


# ---------------------------------------------------------------------------
# extract_tarball_to
# ---------------------------------------------------------------------------


def test_extract_tarball_to(tmp_path):
    # Build a small gzipped tarball in memory.
    buf = io.BytesIO()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo(name="tool/bin/script.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.write(gzip.compress(raw.getvalue()))
    buf.seek(0)

    extract_path = tmp_path / "extracted"
    extract_tarball_to(buf, str(extract_path))

    assert (extract_path / "tool/bin/script.py").read_bytes() == b"print('hi')\n"


def test_extract_tarball_to_allows_absolute_symlinks(tmp_path):
    # miniforge/conda cache tarballs contain absolute-path symlinks (e.g.
    # ``miniforge3-*/_conda``). Python 3.14's default ``data`` extract filter
    # rejects those with ``AbsoluteLinkError``; extraction must use
    # ``filter="fully_trusted"`` or every cache-hit conda tool install breaks.
    buf = io.BytesIO()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name="tool/_conda")
        info.type = tarfile.SYMTYPE
        info.linkname = "/opt/conda/bin/conda"  # absolute target
        tar.addfile(info)
    buf.write(gzip.compress(raw.getvalue()))
    buf.seek(0)

    extract_path = tmp_path / "extracted"
    extract_tarball_to(buf, str(extract_path))  # must not raise AbsoluteLinkError

    link = extract_path / "tool" / "_conda"
    assert link.is_symlink()
    assert link.readlink().as_posix() == "/opt/conda/bin/conda"


# ---------------------------------------------------------------------------
# BuildUploadsStorage (direct artifact upload)
# ---------------------------------------------------------------------------


def make_uploads_storage(**overrides):
    kwargs = {"bucket_name": "uploads", "region_name": "us-east-1"}
    kwargs.update(overrides)
    with mock.patch("builder.storage.boto3.client"):
        return BuildUploadsStorage(**kwargs)


def test_uploads_open_returns_object_body_as_bytesio():
    storage = make_uploads_storage()
    storage._client.get_object.return_value = {"Body": io.BytesIO(b"PK\x03\x04zip")}

    fd = storage.open("uploads/pip/42/artifacts.zip")

    assert fd.read() == b"PK\x03\x04zip"


def test_uploads_open_reads_from_the_uploads_bucket():
    storage = make_uploads_storage()
    storage._client.get_object.return_value = {"Body": io.BytesIO(b"zip")}

    storage.open("uploads/pip/42/artifacts.zip")

    storage._client.get_object.assert_called_once_with(
        Bucket="uploads", Key="uploads/pip/42/artifacts.zip"
    )


def test_uploads_storage_is_read_only():
    # The scoped credentials only grant GetObject on the uploaded key; exposing
    # a sync/delete would be a bug waiting to be called.
    storage = make_uploads_storage()
    assert not hasattr(storage, "rclone_sync_directory")
    assert not hasattr(storage, "delete_directory")

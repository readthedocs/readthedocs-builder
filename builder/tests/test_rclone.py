"""
Tests for the rclone wrapper.

Ported from ``readthedocs.storage.rclone`` (no dedicated upstream test file —
it was exercised via ``test_build_storage.py``). ``subprocess.run`` is stubbed
so command construction is asserted without invoking a real ``rclone``.

The ``clean_name`` / ``safe_join`` path-traversal helpers (advisory
GHSA-5w8m-r7jm-mhp9) get direct unit tests.
"""

import pathlib
from unittest import mock

import pytest

from builder.rclone import BaseRClone
from builder.rclone import RCloneS3Remote
from builder.rclone import _safe_join
from builder.rclone import clean_name
from builder.rclone import safe_join


def make_s3(**overrides):
    kwargs = {
        "bucket_name": "media-bucket",
        "access_key_id": "AKIA",
        "secret_access_key": "secret",
        "region": "us-east-1",
    }
    kwargs.update(overrides)
    return RCloneS3Remote(**kwargs)


# ---------------------------------------------------------------------------
# clean_name
# ---------------------------------------------------------------------------


def test_clean_name_normalizes_windows_paths():
    assert clean_name("a\\b\\c") == "a/b/c"


def test_clean_name_preserves_trailing_slash():
    assert clean_name("a/b/") == "a/b/"


def test_clean_name_empty_becomes_empty_string():
    assert clean_name(".") == ""
    assert clean_name("") == ""


def test_clean_name_accepts_pathlib():
    assert clean_name(pathlib.PurePosixPath("a/b")) == "a/b"


# ---------------------------------------------------------------------------
# safe_join — path traversal protection
# ---------------------------------------------------------------------------


def test_safe_join_joins_within_base():
    assert safe_join("bucket", "html/index.html") == "bucket/html/index.html"


def test_safe_join_preserves_trailing_slash():
    assert safe_join("bucket", "html/") == "bucket/html/"


def test_safe_join_collapses_relative_segments():
    assert safe_join("bucket", "html/../css/main.css") == "bucket/css/main.css"


@pytest.mark.parametrize("evil", ["../outside", "../../etc/passwd", "html/../../escape"])
def test_safe_join_rejects_escaping_the_base(evil):
    with pytest.raises(ValueError):
        safe_join("bucket", evil)


def test_safe_join_resolving_back_to_base_gets_a_trailing_slash():
    # Multi-segment paths that normalize back to the base return ``base/``.
    assert _safe_join("bucket", "sub", "..") == "bucket/"


# ---------------------------------------------------------------------------
# BaseRClone.get_target / execute / sync
# ---------------------------------------------------------------------------


def test_get_target_uses_on_the_fly_remote():
    remote = make_s3()
    # ``:s3:`` declares the remote inline (no config file).
    assert remote.get_target("prefix/dir") == ":s3:media-bucket/prefix/dir"


def test_execute_builds_the_command():
    remote = make_s3()
    with mock.patch("builder.rclone.subprocess.run") as run:
        run.return_value = mock.MagicMock(stdout=b"", stderr=b"", returncode=0)
        remote.execute("sync", args=["src", ":s3:bucket/dst"], options=["--dry-run"])

    command = run.call_args[0][0]
    assert command[0] == "rclone"
    assert command[1] == "sync"
    # default options, then extra options, then a ``--`` separator, then args.
    assert "--dry-run" in command
    assert command[-3:] == ["--", "src", ":s3:bucket/dst"]
    assert "--transfers=8" in command


def test_execute_passes_env_vars_and_checks():
    remote = make_s3()
    with mock.patch("builder.rclone.subprocess.run") as run:
        run.return_value = mock.MagicMock(stdout=b"", stderr=b"", returncode=0)
        remote.execute("sync", args=["a", "b"])

    _, kwargs = run.call_args
    assert kwargs["check"] is True
    assert kwargs["capture_output"] is True
    # Credentials ride in the environment, not argv.
    assert kwargs["env"]["RCLONE_S3_ACCESS_KEY_ID"] == "AKIA"


def test_sync_targets_the_remote():
    remote = make_s3()
    with mock.patch.object(remote, "execute") as execute:
        remote.sync("/local/src", "prefix/dir")
    execute.assert_called_once_with("sync", args=["/local/src", ":s3:media-bucket/prefix/dir"])


# ---------------------------------------------------------------------------
# RCloneS3Remote — env vars & target path
# ---------------------------------------------------------------------------


def test_s3_env_vars_include_core_credentials():
    remote = make_s3()
    env = remote.env_vars
    assert env["RCLONE_S3_PROVIDER"] == "AWS"
    assert env["RCLONE_S3_ACCESS_KEY_ID"] == "AKIA"
    assert env["RCLONE_S3_SECRET_ACCESS_KEY"] == "secret"
    assert env["RCLONE_S3_REGION"] == "us-east-1"
    assert env["RCLONE_S3_LOCATION_CONSTRAINT"] == "us-east-1"


def test_s3_session_token_is_optional():
    assert "RCLONE_S3_SESSION_TOKEN" not in make_s3().env_vars
    assert make_s3(session_token="tok").env_vars["RCLONE_S3_SESSION_TOKEN"] == "tok"


def test_s3_acl_and_endpoint_are_optional():
    remote = make_s3(acl="private", endpoint="http://localhost:9000")
    assert remote.env_vars["RCLONE_S3_ACL"] == "private"
    assert remote.env_vars["RCLONE_S3_ENDPOINT"] == "http://localhost:9000"


def test_s3_target_path_prepends_the_bucket():
    remote = make_s3()
    assert remote._get_target_path("html/index.html") == "media-bucket/html/index.html"


def test_s3_target_path_rejects_traversal():
    remote = make_s3()
    with pytest.raises(ValueError):
        remote._get_target_path("../escape")


# ---------------------------------------------------------------------------
# BaseRClone defaults
# ---------------------------------------------------------------------------


def test_base_rclone_get_target_path_is_abstract():
    with pytest.raises(NotImplementedError):
        BaseRClone()._get_target_path("x")

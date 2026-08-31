"""
Wrapper around the rclone command.

See https://rclone.org/docs.

Ported from ``readthedocs.storage.rclone``, kept as close to the original as
possible. Differences, all forced by the runner having no Django:

- ``safe_join`` / ``clean_name`` are inlined below instead of imported from
  django-storages (``storages.utils``).
- ``RCloneLocal`` is dropped — the runner only ever talks to S3 (dev included,
  via ``AWS_S3_ENDPOINT_URL`` pointing at rustfs).

This module runs *inside* the build container: the runner uploads its own
artifacts, and the worker stays out of the artifact path. The
``readthedocs/build`` images don't ship rclone, so the worker bind-mounts the
host's binary onto ``/usr/local/bin/rclone`` — hence plain ``rclone`` resolves
here off PATH. See ``worker.docker.HOST_RCLONE_PATH``.

TODO: install rclone in the ``readthedocs/build`` images
(readthedocs-docker-images) and drop the bind-mount. Keep the upload in the
container either way.
"""

import os
import pathlib
import posixpath
import subprocess

import structlog


log = structlog.get_logger(__name__)


class SuspiciousFileOperation(Exception):
    """
    Stand-in for ``django.core.exceptions.SuspiciousFileOperation``.

    Same name/semantics as the Django exception the upstream storage code
    raises; defined here because the runner has no Django.
    """


def clean_name(name):
    """
    Normalize the name.

    Includes cleaning up Windows style paths, ensuring an ending trailing slash,
    and coercing from pathlib.PurePath.

    Ported verbatim from ``storages.utils.clean_name``.
    """
    if isinstance(name, pathlib.PurePath):
        name = str(name)

    # Normalize Windows style paths
    clean_name = posixpath.normpath(name).replace("\\", "/")

    # os.path.normpath() can strip trailing slashes so we implement
    # a workaround here.
    if name.endswith("/") and not clean_name.endswith("/"):
        # Add a trailing slash as it was stripped.
        clean_name += "/"

    # Given an empty string, os.path.normpath() will return ., which we don't want
    if clean_name == ".":
        clean_name = ""

    return clean_name


def _safe_join(base, *paths):
    """
    A version of django.utils._os.safe_join for S3 paths.

    Ported verbatim from ``storages.utils.safe_join``.
    """
    base_path = base
    base_path = base_path.rstrip("/")
    paths = list(paths)

    final_path = base_path + "/"
    for path in paths:
        _final_path = posixpath.normpath(posixpath.join(final_path, path))
        # posixpath.normpath() strips the trailing /. Add it back.
        if path.endswith("/") or _final_path + "/" == final_path:
            _final_path += "/"
        final_path = _final_path
    if final_path == base_path:
        final_path += "/"

    # Ensure final_path starts with base_path and that the next character after
    # the base path is /.
    base_path_len = len(base_path)
    if not final_path.startswith(base_path) or final_path[base_path_len] != "/":
        raise ValueError("the joined path is located outside of the base path component")

    return final_path.lstrip("/")


def safe_join(base, path):
    """
    Safely join paths, ensuring that the final path isn't outside the directory.

    We use clean_name before using safe_join, replicating the logic from
    django-storages. This ensures that our path traversal protections can't be
    bypassed, see GHSA-5w8m-r7jm-mhp9 for more information.

    Ported from ``readthedocs.storage.utils.safe_join``.
    """
    return _safe_join(base, clean_name(path))


class BaseRClone:
    """
    RClone base class.

    This class allows you to interact with an rclone remote without
    a configuration file, the remote declaration and its options
    are passed in the command itself.

    :param remote_type: You can see the full list of supported providers at
     https://rclone.org/#providers.
    :param rclone_bin: Binary name or path to the rclone binary.
     Defaults to ``rclone``.
    :param default_options: Options passed to the rclone command.
    :parm env_vars: Environment variables used when executing the rclone command.
     Useful to pass secrets to the ``rclone`  command, since all arguments and
     options will be logged.
    """

    remote_type = None
    rclone_bin = "rclone"
    default_options = [
        # Number of file transfers to run in parallel.
        # Default value is 4.
        "--transfers=8",
        # Skip based on checksum (if available) & size, not mod-time & size.
        "--checksum",
        "--verbose",
        # Retry some times before failing
        # (3 is the default, but making it explicit here)
        "--retries=3",
        # Wait 1 second between each retry
        "--retries-sleep=1s",
    ]
    env_vars = {}

    def _get_target_path(self, path):
        """
        Get the final target path for the remote.

        .. note::

           This doesn't include the remote type,
           this is just the destination path.
        """
        raise NotImplementedError

    def get_target(self, path):
        """
        Get the proper target using the current remote type.

        We start the remote with `:` to create it on the fly,
        instead of having to create a configuration file.
        See https://rclone.org/docs/#backend-path-to-dir.

        :param path: Path to the remote target.
        """
        path = self._get_target_path(path)
        return f":{self.remote_type}:{path}"

    def execute(self, subcommand, args, options=None):
        """
        Execute an rclone subcommand.

        :param subcommand: Name of the subcommand.
        :param list args: List of positional arguments passed the to command.
        :param list options: List of options passed to the command.
        """
        options = options or []
        command = [
            self.rclone_bin,
            subcommand,
            *self.default_options,
            *options,
            "--",
            *args,
        ]
        env = os.environ.copy()
        env.update(self.env_vars)
        log.info("Executing rclone command.", command=command)
        result = subprocess.run(
            command,
            capture_output=True,
            env=env,
            check=True,
        )
        log.debug(
            "rclone execution finished.",
            stdout=result.stdout.decode(),
            stderr=result.stderr.decode(),
            exit_code=result.returncode,
        )
        return result

    def sync(self, source, destination):
        """
        Run the `rclone sync` command.

        See https://rclone.org/commands/rclone_sync/.

        :params source: Local path to the source directory.
        :params destination: Remote path to the destination directory.
        """
        return self.execute("sync", args=[source, self.get_target(destination)])


class RCloneS3Remote(BaseRClone):
    """
    RClone remote implementation for S3.

    All secrets will be passed as environ variables to the rclone command.

    See https://rclone.org/s3/ and https://rclone.org/s3/#configuration.

    :params bucket_name: Name of the S3 bucket.
    :params access_key_id: AWS access key id.
    :params secret_access_key: AWS secret access key.
    :params session_token: AWS session token,
     useful for temporary credentials from AWS STS.
    :params region: AWS region.
    :params provider: S3 provider, defaults to ``AWS``.
     Useful to use rustfs/Minio during development.
     See https://rclone.org/s3/#s3-provider.
    :param acl: Canned ACL used when creating buckets and storing or copying objects.
     See https://rclone.org/s3/#s3-acl.
    :param endpoint: Custom S3 endpoint, useful for development.
    """

    remote_type = "s3"

    def __init__(
        self,
        bucket_name,
        access_key_id,
        secret_access_key,
        region,
        provider="AWS",
        session_token=None,
        acl=None,
        endpoint=None,
    ):
        # rclone S3 options passed as env vars.
        # https://rclone.org/s3/#standard-options.
        self.env_vars = {
            "RCLONE_S3_PROVIDER": provider,
            "RCLONE_S3_ACCESS_KEY_ID": access_key_id,
            "RCLONE_S3_SECRET_ACCESS_KEY": secret_access_key,
            "RCLONE_S3_REGION": region,
            "RCLONE_S3_LOCATION_CONSTRAINT": region,
        }
        if session_token:
            self.env_vars["RCLONE_S3_SESSION_TOKEN"] = session_token
        if acl:
            self.env_vars["RCLONE_S3_ACL"] = acl
        if endpoint:
            self.env_vars["RCLONE_S3_ENDPOINT"] = endpoint
        self.bucket_name = bucket_name

    def _get_target_path(self, path):
        """Overridden to prepend the bucket name to the path."""
        return safe_join(self.bucket_name, path)

"""
Git VCS backend for the build runner.

Ported from ``readthedocs.vcs_support.base`` and
``readthedocs.vcs_support.backends.git``. Combines the abstract
``BaseVCS`` and the concrete git ``Backend`` into a single module since git
is the only supported VCS.

All git invocations go through :class:`builder.environments.BuildEnvironment`,
so VCS errors flow back through the same recording / API plumbing as any
other build command.

Submodule support depends on :mod:`builder.config` (for the ``ALL`` sentinel
and the parsed ``submodules`` block); until that's ported, only the
``ALL`` sentinel is needed and is imported from :mod:`builder.constants`.
"""

import os
import re
from typing import Iterable
from urllib.parse import urlparse

import structlog

from builder.constants import ALL
from builder.constants import BRANCH
from builder.constants import TAG
from builder.exceptions import BuildCancelled
from builder.exceptions import BuildUserError
from builder.exceptions import RepositoryError
from builder.filesystem import safe_rmtree
from builder.lsremote import parse_lsremote
from builder.refspec import get_remote_fetch_refspec


log = structlog.get_logger(__name__)


class VCSVersion:
    """A version (branch or tag) discovered in a remote VCS."""

    def __init__(self, repository, identifier, verbose_name):
        self.repository = repository
        self.identifier = identifier
        self.verbose_name = verbose_name

    def __repr__(self):
        return f"<VCSVersion: {self.repository.repo_url}:{self.verbose_name}>"


class Backend:
    """
    Git VCS backend.

    Owns a working directory under the project's checkout path, drives ``git``
    via the build environment, and exposes the methods consumed by
    :class:`builder.director.BuildDirector`: ``update``, ``checkout``,
    ``update_submodules``, ``has_ssh_key_with_write_access``, ``commit``,
    ``get_default_branch``, and ``lsremote``.
    """

    fallback_branch = "master"
    repo_depth = 50

    def __init__(self, project, version, environment, use_token=False, **kwargs):
        self.project = project
        self.version = version
        self.environment = environment
        self.name = project.name
        self.default_branch = project.default_branch
        self.working_dir = project.checkout_path(version.slug)
        self.use_token = use_token
        # ``clean_repo`` rewrites legacy http://github.com URLs to https://.
        self.repo_url = self._get_clone_url(project.clean_repo)

    # ---- Working directory helpers ----

    def check_working_dir(self) -> None:
        if not os.path.exists(self.working_dir):
            # Create the dir through the build environment so it's wrapped in
            # ``runuser`` and owned by the build user.
            # ``cwd="/"`` is just a guaranteed-existing directory to spawn from (the path is absolute).
            self.environment.run(
                "mkdir",
                "--parents",
                self.working_dir,
                cwd="/",
                record=False,
                # Everything after this runs with the checkout as its cwd.
                warn_only=False,
            )

    def make_clean_working_dir(self) -> None:
        """Ensure the working dir exists and is empty."""
        safe_rmtree(self.working_dir, ignore_errors=True)
        self.check_working_dir()

    def _get_clone_url(self, repo_url: str) -> str:
        """Substitute the clone token into HTTPS URLs when ``use_token=True``."""
        if repo_url and repo_url.startswith(("https://", "http://")) and self.use_token:
            parsed = urlparse(repo_url)
            return f"{parsed.scheme}://$READTHEDOCS_GIT_CLONE_TOKEN@{parsed.netloc}{parsed.path}"
        return repo_url

    # ---- Command runner ----

    def run(self, *cmd, **kwargs):
        """
        Run a git command in the working directory and translate failures.

        Failures bubble up as :class:`RepositoryError` so the wrapping director
        treats them as user-facing repo errors. Cancellation is preserved.
        """
        kwargs.update({"cwd": self.working_dir})
        try:
            build_cmd = self.environment.run(*cmd, **kwargs)
        except BuildCancelled as exc:
            raise BuildCancelled(message_id=BuildCancelled.CANCELLED_BY_USER) from exc
        except BuildUserError as exc:
            raise RepositoryError(message_id=RepositoryError.GENERIC) from exc
        return (build_cmd.exit_code, build_cmd.output, build_cmd.error)

    # ---- High-level operations ----

    def update(self):
        """Clone the repository, then fetch the relevant ref."""
        self.check_working_dir()

        # Honour a project-level custom checkout command if configured.
        if self.project.git_checkout_command:
            if isinstance(self.project.git_checkout_command, list):
                for cmd in self.project.git_checkout_command:
                    # ``escape_command=False`` is required so env vars
                    # (e.g. ``$READTHEDOCS_GIT_CLONE_TOKEN``) get expanded.
                    self.run(*cmd.split(), escape_command=False)
                return

        self.clone()
        return self.fetch()

    def clone(self):
        """Shallow-clone the repository into the working directory."""
        cmd = ["git", "clone", "--depth", "1", self.repo_url, "."]
        try:
            return self.run(*cmd)
        except RepositoryError as exc:
            message_id = RepositoryError.CLONE_ERROR_WITH_PRIVATE_REPO_NOT_ALLOWED
            if self.environment.allow_private_repos:
                message_id = RepositoryError.CLONE_ERROR_WITH_PRIVATE_REPO_ALLOWED
            raise RepositoryError(message_id=message_id) from exc

    def get_remote_fetch_refspec(self) -> str | None:
        """
        Build the refspec for ``git fetch`` based on the version type.

        Delegates to ``builder.refspec`` so the build and the worker's
        bootstrap sparse-clone stay in agreement.
        """
        return get_remote_fetch_refspec(
            version_type=self.version.type,
            verbose_name=self.version.verbose_name,
            identifier=self.version.identifier,
            machine=self.version.machine,
            slug=self.version.slug,
            is_github=self.project.is_github_project,
            is_gitlab=self.project.is_gitlab_project,
        )

    def fetch(self):
        """Fetch the relevant ref(s) into the working directory."""
        cmd = [
            "git",
            "fetch",
            "origin",
            "--force",
            "--prune",
            "--prune-tags",
            "--depth",
            str(self.repo_depth),
        ]

        # If we're building "latest" and the project has no explicit default
        # branch, fetch HEAD (a symref to the remote's default branch).
        use_default_branch = self.version.is_machine_latest and not self.project.default_branch
        if use_default_branch:
            cmd.append("HEAD")
        else:
            remote_reference = self.get_remote_fetch_refspec()
            if remote_reference:
                cmd.append(remote_reference)
            elif not self.version.machine:
                log.warning(
                    "Git fetch: Could not decide a remote reference for version. "
                    "Is it an empty default branch?",
                    project_slug=self.project.slug,
                    verbose_name=self.version.verbose_name,
                    version_type=self.version.type,
                    version_identifier=self.version.identifier,
                )

        return self.run(*cmd)

    def has_ssh_key_with_write_access(self) -> bool:
        """
        Probe whether the deploy SSH key has write access via ``git push --dry-run``.

        Logic ported verbatim from upstream: temporarily add an SSH-form
        remote, run a 10s-timeout dry-run push, classify the result by
        well-known error strings, then always remove the temporary remote.
        """
        remote_name = "rtd-test-ssh-key"
        ssh_url = self.project.repo or ""
        if ssh_url.startswith("http"):
            parsed = urlparse(ssh_url)
            ssh_url = f"git@{parsed.netloc}:{parsed.path.lstrip('/')}"

        try:
            self.run("git", "remote", "add", remote_name, ssh_url, record=False)
            code, stdout, stderr = self.run(
                "timeout",
                "10s",
                "git",
                "push",
                "--dry-run",
                remote_name,
                record=False,
                demux=True,
            )

            if code == 0:
                return True

            if "ERROR: This repository was archived so it is read-only" in stderr:
                return True

            # Empty default branch: assume write access; later steps will fail
            # for unrelated reasons if the key is actually read-only.
            if re.search(r"error: src refspec refs/heads/\w does not match any", stderr):
                return True

            if re.search(r"ERROR: Permission to .* denied to deploy key", stderr):
                return False

            errors_read_access_only = [
                "ERROR: The key you are authenticating with has been marked as read only",
                "ERROR: Write access to repository not granted",
                "git@github.com: Permission denied (publickey).",
                "ERROR: The repository owner has an IP allow list enabled",
                "ERROR: This deploy key does not have write access to this project.",
                "remote: This deploy key does not have write access to this project.",
                "fatal: Could not read from remote repository.",
                "You need the Git 'GenericContribute' permission to perform this action.",
            ]
            for pattern in errors_read_access_only:
                if pattern in stderr:
                    return False

            if "fatal: could not read Username for" in stderr:
                log.error(
                    "Invalid repo URL for SSH key check.",
                    project_slug=self.project.slug,
                    repo_url=self.project.repo,
                    ssh_url=ssh_url,
                    exit_code=code,
                    stdout=stdout,
                    stderr=stderr,
                )
                return False

            log.error(
                "Unknown error when checking SSH key access.",
                project_slug=self.project.slug,
                exit_code=code,
                stdout=stdout,
                stderr=stderr,
            )
            return False
        finally:
            self.run("git", "remote", "remove", remote_name, record=False)

    # ---- Submodules ----

    def are_submodules_available(self, config) -> bool:
        """Should the submodule checkout step run for this build?"""
        submodules_in_config = config.submodules.exclude != ALL or config.submodules.include
        if not submodules_in_config:
            return False
        return any(self.submodules)

    def get_available_submodules(self, config) -> tuple[bool, list]:
        """
        Resolve which submodules to fetch for this build.

        Returns ``(should_run, paths)`` where ``paths`` is empty when "all".
        """
        if config.submodules.exclude == ALL:
            return False, []

        if config.submodules.exclude:
            submodules = list(self.submodules)
            for sub_path in config.submodules.exclude:
                path = sub_path.rstrip("/")
                try:
                    submodules.remove(path)
                except ValueError:
                    pass
            if not submodules:
                return False, []
            return True, submodules

        if config.submodules.include == ALL:
            return True, []

        if config.submodules.include:
            return True, config.submodules.include

        return False, []

    def update_submodules(self, config):
        """Sync and check out submodules selected by ``config``."""
        if self.are_submodules_available(config):
            valid, submodules = self.get_available_submodules(config)
            if valid:
                self.checkout_submodules(submodules, config.submodules.recursive)

    def checkout_submodules(self, submodules: list[str], recursive: bool):
        self.run("git", "submodule", "sync")
        cmd = ["git", "submodule", "update", "--init", "--force"]
        if recursive:
            cmd.append("--recursive")
        cmd.append("--")
        cmd += submodules
        self.run(*cmd)

    @property
    def submodules(self) -> Iterable[str]:
        r"""
        Iterate submodule paths declared in ``.gitmodules`` without initializing them.

        Uses ``git config --null --get-regexp`` so paths and keys with spaces
        survive parsing. Yields paths only; missing-path entries are skipped.
        """
        exit_code, stdout, _ = self.run(
            "git",
            "config",
            "--null",
            "--file",
            ".gitmodules",
            "--get-regexp",
            r"^submodule\..*\.path$",
            record=False,
        )
        if exit_code != 0:
            return

        keys_and_values = stdout.split("\0")
        for kv in keys_and_values:
            try:
                key, value = kv.split("\n", maxsplit=1)
            except ValueError:
                log.warning("Wrong key and value format.", key_and_value=kv)
                continue
            if key.endswith(".path"):
                yield value
            else:
                log.warning("Unexpected key extracted from .gitmodules.", key=key)

    # ---- Checkout / refs ----

    def checkout_revision(self, revision):
        try:
            return self.run("git", "checkout", "--force", revision)
        except RepositoryError as exc:
            raise RepositoryError(
                message_id=RepositoryError.FAILED_TO_CHECKOUT,
                format_values={"revision": revision},
            ) from exc

    def checkout(self, identifier=None):
        """Check out ``identifier`` (or stay on the cloned default branch)."""
        self.check_working_dir()

        # If a custom git_checkout_command ran during update(), nothing to do.
        if self.project.git_checkout_command:
            return None

        if not identifier:
            return None

        identifier = self.find_ref(identifier)
        return self.checkout_revision(identifier)

    def find_ref(self, ref: str) -> str:
        """Resolve ``ref`` to an ``origin/<branch>`` form when possible."""
        if ref.startswith("origin/"):
            return ref
        if self.ref_exists("refs/remotes/origin/" + ref):
            return "origin/" + ref
        return ref

    def ref_exists(self, ref: str) -> bool:
        exit_code, _, _ = self.run(
            "git", "show-ref", "--verify", "--quiet", "--", ref, record=False
        )
        return exit_code == 0

    @property
    def commit(self) -> str:
        _, stdout, _ = self.run("git", "rev-parse", "HEAD", record=False)
        return stdout.strip()

    def get_default_branch(self) -> str:
        """Resolve the remote's default branch via ``git symbolic-ref``."""
        cmd = ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
        _, stdout, _ = self.run(*cmd, demux=True, record=False)
        return stdout.strip().removeprefix("origin/")

    def lsremote(self, include_tags=True, include_branches=True):
        """List remote refs without cloning. Returns ``(branches, tags)``."""
        if not include_tags and not include_branches:
            return [], []

        extra_args = []
        if include_tags:
            extra_args.append("--tags")
        if include_branches:
            extra_args.append("--heads")

        cmd = ["git", "ls-remote", *extra_args, self.repo_url]

        self.check_working_dir()
        exit_code, stdout, _ = self.run(*cmd, demux=True, record=False)

        if exit_code != 0:
            raise RepositoryError(message_id=RepositoryError.FAILED_TO_GET_VERSIONS)

        branches, tags = parse_lsremote(stdout)
        return (
            [VCSVersion(self, identifier, verbose_name) for identifier, verbose_name in branches],
            [VCSVersion(self, identifier, verbose_name) for identifier, verbose_name in tags],
        )


def parse_version_from_ref(ref: str) -> tuple[str, str]:
    """Split a Git ref into ``(name, type)`` where type is ``BRANCH`` or ``TAG``."""
    heads_prefix = "refs/heads/"
    tags_prefix = "refs/tags/"
    if ref.startswith(heads_prefix):
        return ref.removeprefix(heads_prefix), BRANCH
    if ref.startswith(tags_prefix):
        return ref.removeprefix(tags_prefix), TAG
    raise ValueError(f"Invalid ref: {ref}")

"""
The build director — orchestrates a single documentation build.

Ported from ``readthedocs.doc_builder.director``. The class is the entry
point used by the runner: it walks VCS checkout, environment setup,
build-tools installation, language environment, and finally the doctype
builder. State that needs to flow between methods lives on a
:class:`TaskData` instance — same pattern as upstream's Celery task,
minus Celery.
"""

import datetime
import os
import pwd
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

import structlog

from builder import settings
from builder.api_client import get_project_ssh_key
from builder.api_models import APIProject
from builder.api_models import APIVersion
from builder.config import CONFIG_FILENAME_REGEX
from builder.config import load as load_config
from builder.config.find import find_one
from builder.constants import BUILD_COMMANDS_OUTPUT_PATH_HTML
from builder.constants import EXTERNAL
from builder.constants import GENERIC
from builder.constants import MESSAGE_PROJECT_SSH_KEY_WITH_WRITE_ACCESS
from builder.environments import DockerBuildEnvironment
from builder.exceptions import BuildUserError
from builder.exceptions import RepositoryError
from builder.filesystem import assert_path_is_inside_docroot
from builder.filesystem import safe_open
from builder.loader import get_builder_class
from builder.python_envs import Conda
from builder.python_envs import UvEnv
from builder.python_envs import Virtualenv
from builder.ssh import GIT_SSH_COMMAND
from builder.ssh import parse_ssh_agent_env
from builder.utils import get_dotted_attribute


log = structlog.get_logger(__name__)


def _build_user_ids() -> tuple[int, int]:
    """
    The uid/gid the build container runs commands as.

    Resolved by name when that user exists in this process's passwd db
    (production), falling back to the configured ids when it doesn't (the dev
    compose service).
    """
    try:
        entry = pwd.getpwnam(settings.RTD_DOCKER_USER)
    except KeyError:
        return settings.RTD_DOCKER_UID, settings.RTD_DOCKER_GID
    return entry.pw_uid, entry.pw_gid


@dataclass
class TaskData:
    """
    Mutable container for state shared across :class:`BuildDirector` methods.

    Mirrors the upstream Celery ``TaskData`` dataclass; removes the Celery-
    specific fields (``start_time``, ``task_executed_at``, etc.) the runner
    doesn't need yet.
    """

    project: APIProject = None
    version: APIVersion = None
    build: dict = field(default_factory=dict)
    api_client: Any = None

    config: Any = None  # BuildConfigV2 once setup_vcs runs.
    production_domain: str = ""
    allow_private_repos: bool = False
    s3_endpoint_url: str | None = None
    # Always the Docker environment in production: build commands must run
    # inside the container, never on the host the runner lives on. Only tests
    # substitute the local ``BuildEnvironment``.
    environment_class: type = DockerBuildEnvironment
    container_name: str | None = None
    # The worker's Docker client, shared so a build opens one connection.
    docker_client: Any = None
    build_director: "BuildDirector | None" = None
    default_branch: str | None = None


class BuildDirector:
    """
    Orchestrates the documentation build steps.

    The director performs all VCS commands, sets up the OS and language
    (Python) environment, installs user dependencies, and invokes the
    builder backend that writes the artifact files.
    """

    def __init__(self, data):
        """
        :param data: shared :class:`TaskData` instance.
        """
        self.data = data
        # ``SSH_AUTH_SOCK``/``SSH_AGENT_PID`` for the deploy-key agent, once
        # ``setup_ssh_agent`` has started it. Empty for public/HTTPS repos.
        self.ssh_agent_env: dict = {}

    def setup_vcs(self):
        """
        Clone the repository, parse ``.readthedocs.yaml``, init submodules.

        Phases:

        1. Make the project's checkout directory.
        2. Build the VCS environment + repository, run a clone+fetch.
        3. Run the user's ``post_checkout`` job (if any).
        4. Read the config file, fail on legacy/invalid forms.
        5. Update submodules per the config.
        """
        # Create the checkout dir through the build environment so it's wrapped
        # in ``runuser`` and owned by the build user.
        if not os.path.exists(self.data.project.doc_path):
            self.vcs_environment.run(
                "mkdir",
                "--parents",
                self.data.project.doc_path,
                cwd="/",
                record=False,
                # The whole build happens under here; failing now beats
                # failing later on a missing cwd.
                warn_only=False,
            )

        if not self.data.project.vcs_class():
            raise RepositoryError(RepositoryError.UNSUPPORTED_VCS)

        # Set up the VCS repository (cloning, fetching, checking out).
        self.vcs_repository = self.data.project.vcs_repo(
            version=self.data.version,
            environment=self.vcs_environment,
        )

        # Load the SSH deploy key into an agent before cloning a private repo.
        self.setup_ssh_agent()

        # We can't run a ``pre_checkout`` job here because the config file
        # isn't loaded yet (the repo isn't cloned). See
        # https://github.com/readthedocs/readthedocs.org/issues/8935 for the
        # design constraint.
        #
        # TODO: now that we are doing a spare-clone we could pass the `pre_checkout` job
        # when calling the runner and execute it here.
        self.checkout()

        self.run_build_job("post_checkout")

        commit = self.vcs_repository.commit
        if commit:
            self.data.build["commit"] = commit

    def create_vcs_environment(self):
        """Build the VCS-time :class:`BuildEnvironment` (no config yet)."""
        self.vcs_environment = self.data.environment_class(
            project=self.data.project,
            version=self.data.version,
            build=self.data.build,
            environment=self.get_vcs_env_vars(),
            record=True,
            api_client=self.data.api_client,
            container_name=self.data.container_name,
            docker_client=self.data.docker_client,
            allow_private_repos=self.data.allow_private_repos,
        )

    def create_build_environment(self):
        """Build the post-config :class:`BuildEnvironment` for build commands."""
        if self.data.build.get("is_uploaded"):
            # Expose only the READTHEDOCS_ environment variables when ``is_upload``
            environment = self.get_rtd_env_vars()
        else:
            environment = self.get_build_env_vars()

        self.build_environment = self.data.environment_class(
            project=self.data.project,
            version=self.data.version,
            config=self.data.config,
            build=self.data.build,
            environment=environment,
            record=True,
            api_client=self.data.api_client,
            container_name=self.data.container_name,
            docker_client=self.data.docker_client,
            allow_private_repos=self.data.allow_private_repos,
        )

    def setup_environment(self):
        """
        Install OS deps, build tools, and the language environment.

        1. ``apt_packages`` (as super-user).
        2. ``build.tools`` (asdf installs).
        3. Language environment (virtualenv / uv venv / conda).
        4. User Python deps via ``python.install``.

        Each phase is bracketed by ``pre_*`` / ``post_*`` ``build.jobs`` hooks.
        """
        language_environment_cls = Virtualenv
        if self.data.config.is_using_conda:
            language_environment_cls = Conda
        elif self.data.config.is_using_uv:
            language_environment_cls = UvEnv

        self.language_environment = language_environment_cls(
            version=self.data.version,
            build_env=self.build_environment,
            config=self.data.config,
        )

        self.run_build_job("pre_system_dependencies")
        self.system_dependencies()
        self.run_build_job("post_system_dependencies")

        self.install_build_tools()

        self.run_build_job("pre_create_environment")
        self.create_environment()
        self.run_build_job("post_create_environment")

        self.run_build_job("pre_install")
        self.install()
        self.run_build_job("post_install")

    def build(self):
        """Build every output format the user asked for + run pre/post hooks."""
        self.run_build_job("pre_build")

        self.build_html()
        self.build_htmlzip()
        self.build_pdf()
        self.build_epub()

        self.run_build_job("post_build")
        self.store_readthedocs_build_yaml()

    # ----- VCS checkout -----

    def checkout(self):
        """Clone, checkout the requested commit/identifier, and load the config file."""
        log.info("Cloning and fetching.")
        self.vcs_repository.update()

        # Business-only: enforce SSH-key-write-access policy. Only matters
        # when the runner is configured against a private-repo-capable
        # instance; the brownouts have already passed (date >= 2025-12-01),
        # so this is now a hard failure when the flag is set.
        #
        # TODO: remove the brownout check completely.
        has_ssh_key_with_write_access = False
        if self.data.allow_private_repos:
            has_ssh_key_with_write_access = self.vcs_repository.has_ssh_key_with_write_access()
            if has_ssh_key_with_write_access != self.data.project.has_ssh_key_with_write_access:
                self.data.api_client.project(self.data.project.pk).patch(
                    {"has_ssh_key_with_write_access": has_ssh_key_with_write_access}
                )

            now = datetime.datetime.now(tz=datetime.timezone.utc)
            hard_failure = now >= datetime.datetime(
                2025, 12, 1, 0, 0, 0, tzinfo=datetime.timezone.utc
            )
            if has_ssh_key_with_write_access:
                if hard_failure and settings.RTD_ENFORCE_BROWNOUTS_FOR_DEPRECATIONS:
                    raise BuildUserError(BuildUserError.SSH_KEY_WITH_WRITE_ACCESS)
                else:
                    self.attach_notification(
                        attached_to=f"project/{self.data.project.pk}",
                        message_id=MESSAGE_PROJECT_SSH_KEY_WITH_WRITE_ACCESS,
                        dismissable=True,
                    )

        # When building "latest" without an explicit default_branch, capture
        # the remote's HEAD branch so the API gets updated post-build.
        is_latest_without_default_branch = (
            self.data.version.is_machine_latest and not self.data.project.default_branch
        )
        if is_latest_without_default_branch:
            self.data.default_branch = self.data.build_director.vcs_repository.get_default_branch()
            log.info(
                "Default branch for the repository detected.",
                default_branch=self.data.default_branch,
            )

        # Skip the explicit checkout when the clone already lands us on the
        # right commit (the default-branch case above).
        if not is_latest_without_default_branch:
            identifier = self.data.version.identifier
            log.info("Checking out.", identifier=identifier)
            self.vcs_repository.checkout(identifier)

        # Resolve which config file to load. The per-build override is
        # commented out upstream pending design work; we mirror that.
        custom_config_file = None
        if not custom_config_file and self.data.version.project.readthedocs_yaml_path:
            custom_config_file = self.data.version.project.readthedocs_yaml_path

        if custom_config_file:
            log.info("Using a custom .readthedocs.yaml file.", path=custom_config_file)

        checkout_path = self.data.project.checkout_path(self.data.version.slug)
        default_config_file = find_one(checkout_path, CONFIG_FILENAME_REGEX)
        final_config_file = custom_config_file or default_config_file

        # Echo the chosen config file's path into the build log so the user
        # can confirm what we're using.
        if final_config_file:
            self.vcs_environment.run(
                "cat",
                final_config_file.replace(checkout_path + "/", ""),
                cwd=checkout_path,
            )

        # ``load_config`` validates the file (version 2, ``build.os`` required,
        # no legacy ``build.image``); upstream's redundant post-load checks for
        # those are dropped — they were unreachable here.
        self.data.config = load_config(
            path=checkout_path,
            readthedocs_yaml_path=custom_config_file,
        )
        self.data.build["config"] = self.data.config.as_dict()
        self.data.build["readthedocs_yaml_path"] = custom_config_file

        self.vcs_repository.update_submodules(self.data.config)

        # Block ``post_checkout`` jobs when the SSH key has write access —
        # otherwise a malicious config could push back to the repo.
        if has_ssh_key_with_write_access and get_dotted_attribute(
            self.data.config, "build.jobs.post_checkout", None
        ):
            raise BuildUserError(BuildUserError.SSH_KEY_WITH_WRITE_ACCESS)

    # ----- System dependencies (build.apt_packages) -----

    def system_dependencies(self):
        """
        Install the user's apt packages as super-user.

        Package names are validated by the config parser; we don't allow
        custom apt options or install-from-path. ``--quiet`` only suppresses
        the progress bar, not package names.
        """
        packages = self.data.config.build.apt_packages
        if packages:
            self.build_environment.run(
                "apt-get",
                "update",
                "--assume-yes",
                "--quiet",
                user=settings.RTD_DOCKER_SUPER_USER,
            )
            # ``--`` ends option parsing so package names that look like
            # options can't sneak through.
            self.build_environment.run(
                "apt-get",
                "install",
                "--assume-yes",
                "--quiet",
                "--",
                *packages,
                user=settings.RTD_DOCKER_SUPER_USER,
            )

    # ----- Language environment -----

    def create_environment(self):
        """Run the user's ``create_environment`` job, or default to the language env."""
        if self.data.config.build.jobs.create_environment is not None:
            self.run_build_job("create_environment")
            return

        # Generic builds run the user's commands, so no Sphinx/MkDocs deps are
        # installed by default. uv is the exception: ``uv venv``/``uv sync``
        # still have to run.
        if self.data.config.doctype == GENERIC and not self.data.config.is_using_uv:
            return

        self.language_environment.setup_base()

    # ----- Install -----

    def install(self):
        """Run the user's ``install`` job, or install RTD core deps + ``python.install``."""
        if self.data.config.build.jobs.install is not None:
            self.run_build_job("install")
            return

        # Same as ``create_environment``: generic builds skip the default
        # dependency install, unless the project is using uv.
        if self.data.config.doctype == GENERIC and not self.data.config.is_using_uv:
            return

        self.language_environment.install_core_requirements()
        self.language_environment.install_requirements()

    # ----- Build -----

    def build_html(self):
        if self.data.config.build.jobs.build.html is not None:
            self.run_build_job("build.html")
            return
        return self.build_docs_class(self.data.config.doctype)

    def build_pdf(self):
        if "pdf" not in self.data.config.formats or self.data.version.type == EXTERNAL:
            return False

        if self.data.config.build.jobs.build.pdf is not None:
            self.run_build_job("build.pdf")
            return

        # MkDocs has no PDF generation; only Sphinx is supported.
        if self.is_type_sphinx():
            return self.build_docs_class("sphinx_pdf")

        return False

    def build_htmlzip(self):
        if "htmlzip" not in self.data.config.formats or self.data.version.type == EXTERNAL:
            return False

        if self.data.config.build.jobs.build.htmlzip is not None:
            self.run_build_job("build.htmlzip")
            return

        if self.is_type_sphinx():
            return self.build_docs_class("sphinx_singlehtmllocalmedia")
        return False

    def build_epub(self):
        if "epub" not in self.data.config.formats or self.data.version.type == EXTERNAL:
            return False

        if self.data.config.build.jobs.build.epub is not None:
            self.run_build_job("build.epub")
            return

        if self.is_type_sphinx():
            return self.build_docs_class("sphinx_epub")
        return False

    def run_build_job(self, job):
        """
        Run the commands listed under ``build.jobs.<job>`` in the user config.

        - ``pre_checkout`` / ``post_checkout`` run in the VCS env (the only
          jobs that can run before the config-aware build env exists).
        - All others run in the build env.

        User commands are intentionally not escaped, are run from the
        repository checkout, and inherit the same env vars as built-in
        commands. The ``runuser`` privilege drop in BuildEnvironment keeps
        them out of root.
        """
        commands = get_dotted_attribute(self.data.config, f"build.jobs.{job}", None)
        if not commands:
            return

        cwd = self.data.project.checkout_path(self.data.version.slug)
        environment = self.vcs_environment
        if job not in ("pre_checkout", "post_checkout"):
            environment = self.build_environment

        for command in commands:
            environment.run(command, escape_command=False, cwd=cwd)

    def check_old_output_directory(self):
        """
        Fail the build if the legacy ``_build/html`` output dir exists.

        Older RTD builds wrote artifacts to ``_build/html`` and some
        long-lived projects hardcoded that path. We've since switched to
        ``_readthedocs/`` so detecting the old path is a strong signal of a
        soon-to-be-broken build.
        """
        command = self.build_environment.run(
            "test",
            "-x",
            "_build/html",
            cwd=self.data.project.checkout_path(self.data.version.slug),
            record=False,
        )
        if command.exit_code == 0:
            log.warning("Directory '_build/html' exists. This may lead to unexpected behavior.")
            raise BuildUserError(BuildUserError.BUILD_OUTPUT_OLD_DIRECTORY_USED)

    def run_build_commands(self):
        """
        Run a generic build's ``build.commands`` in sequence.

        After tools that modify shims (``pip install``, ``conda install``,
        ``mamba install``, ``poetry install``, ``cargo install``) we
        ``asdf reshim`` so newly installed binaries appear on PATH.
        """
        python_reshim_commands = (
            {"pip", "install"},
            {"conda", "create"},
            {"conda", "install"},
            {"mamba", "create"},
            {"mamba", "install"},
            {"poetry", "install"},
        )
        rust_reshim_commands = ({"cargo", "install"},)

        cwd = self.data.project.checkout_path(self.data.version.slug)
        environment = self.build_environment
        for command in self.data.config.build.commands:
            environment.run(command, escape_command=False, cwd=cwd)

            # ``pip install`` etc. may install a CLI; reshim so it's on PATH.
            for python_reshim_command in python_reshim_commands:
                if python_reshim_command.issubset(command.split()):
                    environment.run(
                        *["asdf", "reshim", "python"],
                        escape_command=False,
                        cwd=cwd,
                        record=False,
                    )

            for rust_reshim_command in rust_reshim_commands:
                if rust_reshim_command.issubset(command.split()):
                    environment.run(
                        *["asdf", "reshim", "rust"],
                        escape_command=False,
                        cwd=cwd,
                        record=False,
                    )

        html_output_path = os.path.join(cwd, BUILD_COMMANDS_OUTPUT_PATH_HTML)
        if not os.path.exists(html_output_path):
            raise BuildUserError(BuildUserError.BUILD_COMMANDS_WITHOUT_OUTPUT)

        # Generic builds always advertise GENERIC as the doctype upstream.
        self.data.version.documentation_type = self.data.config.doctype
        self.store_readthedocs_build_yaml()

    def install_build_tools(self):
        """
        Install every ``build.tools`` version, preferring the S3 cache.

        For each tool in the user's config we:

        1. Look up the tarball in the build-tools cache (key
           ``<build_os>-<tool>-<full_version>.tar.gz``). The cache is
           populated out-of-band by an RTD-managed compile job.
        2. On hit: stream the tarball, extract under the project's doc
           path, then ``mv`` into ``~/.asdf/installs/<tool>/<version>``.
        3. On miss: ``asdf install`` (compiles or downloads).

        Either way we then ``asdf global`` and ``asdf reshim`` so the
        version is on PATH. UV plugin setup for Python is preserved.
        """
        from builder.storage import StorageType
        from builder.storage import get_storage

        build_tools_storage = get_storage(
            build_id=self.data.build["id"],
            api_client=self.data.api_client,
            storage_type=StorageType.build_tools,
            endpoint_url=self.data.s3_endpoint_url,
        )

        for tool, version in self.data.config.build.tools.items():
            full_version = version.full_version  # e.g. 3.9 -> 3.9.7

            # The cache key uses the concrete OS, not the ``ubuntu-lts-latest``
            # alias, so the same alias can roll forward without invalidating.
            build_os = self.data.config.build.os
            if build_os == "ubuntu-lts-latest":
                _, build_os = settings.RTD_DOCKER_BUILD_SETTINGS["os"]["ubuntu-lts-latest"].split(
                    ":"
                )

            tool_path = f"{build_os}-{tool}-{full_version}.tar.gz"
            tool_version_cached = build_tools_storage.exists(tool_path)

            if tool_version_cached:
                from builder.storage import extract_tarball_to

                remote_fd = build_tools_storage.open(tool_path, mode="rb")
                # Extract on the shared volume between host and container; the
                # path layout matches asdf's ``installs/<tool>/<version>``.
                extract_path = os.path.join(self.data.project.doc_path, "tools")
                extract_tarball_to(remote_fd, extract_path)

                # ``extract_tarball_to`` unpacks in the (root) runner, so the
                # tree is root-owned. Hand it to the build user before the
                # docs-user ``mv`` (and later asdf/pip writes) can touch it.
                self.build_environment.run(
                    "chown",
                    "--recursive",
                    f"{settings.RTD_DOCKER_USER}:{settings.RTD_DOCKER_USER}",
                    extract_path,
                    user="root",
                    record=False,
                )

                # Move the extracted ``<full_version>`` directory into asdf's
                # canonical install location.
                cmd = [
                    "mv",
                    f"{extract_path}/{full_version}",
                    os.path.join(
                        settings.RTD_DOCKER_WORKDIR,
                        f".asdf/installs/{tool}/{full_version}",
                    ),
                ]
                self.build_environment.run(*cmd, record=False)
            else:
                log.debug(
                    "Cached version for tool not found.",
                    os=self.data.config.build.os,
                    tool=tool,
                    full_version=full_version,
                    tool_path=tool_path,
                )
                self.build_environment.run("asdf", "install", tool, full_version)

            # Always set as global + reshim so the new version is on PATH.
            self.build_environment.run("asdf", "global", tool, full_version)
            self.build_environment.run("asdf", "reshim", tool, record=False)

            # When the user opts into ``method: uv``, install ``uv`` via the
            # uv asdf plugin.
            if tool == "python" and self.data.config.is_using_uv:
                self.build_environment.run("asdf", "plugin", "add", "uv", record=True)
                self.build_environment.run("asdf", "install", "uv", "latest", record=True)
                self.build_environment.run("asdf", "global", "uv", "latest", record=True)

            # Bootstrap virtualenv + setuptools into the just-installed Python.
            # Skipped on a cache hit: the cached tarball is built by our own
            # script with these already installed, so reinstalling is wasted
            # work (and surprising for ``build.commands`` users, who manage
            # their own environment). Only compiled-on-the-fly pythons need it.
            # Conda/mamba manage their own environment and are skipped too.
            if all(
                [
                    tool == "python",
                    not tool_version_cached,
                    self.data.config.python_interpreter not in ("conda", "mamba"),
                ]
            ):
                # Cap setuptools when the project uses ``setup.py install`` —
                # newer setuptools removed that command and would break the build.
                # https://github.com/readthedocs/readthedocs.org/issues/8659
                setuptools_version = (
                    "setuptools<58.3.0"
                    if self.data.config.is_using_setup_py_install
                    else "setuptools"
                )
                self.build_environment.run(
                    "python",
                    "-mpip",
                    "install",
                    "-U",
                    "virtualenv",
                    setuptools_version,
                )

    # ----- Helpers -----

    def build_docs_class(self, builder_class):
        """
        Instantiate and run a builder backend.

        Returns the builder's success status. For the canonical doctype
        (``self.data.config.doctype``), also calls ``show_conf`` and
        records the resolved doctype back onto the version (e.g. Sphinx may
        narrow it to ``sphinx_htmldir`` based on the config).
        """
        if builder_class == GENERIC:
            return

        builder = get_builder_class(builder_class)(
            build_env=self.build_environment,
            python_env=self.language_environment,
        )

        if builder_class == self.data.config.doctype:
            builder.show_conf()
            self.data.version.documentation_type = builder.get_final_doctype()

        success = builder.build()
        return success

    def _add_git_ssh_command_env_var(self, env):
        """Disable strict host-key prompting when private repos are allowed."""
        if self.data.allow_private_repos:
            env.update({"GIT_SSH_COMMAND": GIT_SSH_COMMAND})

    def setup_ssh_agent(self):
        """
        Load the project's SSH deploy key into an ssh-agent for private clones.

        Fetch the private key from the API, write it to a
        locked-down file *inside the docroot*, start an ssh-agent and
        ``ssh-add`` through the VCS environment (so the agent lives in the
        build container, where every git command runs), and inject
        ``SSH_AUTH_SOCK``/``SSH_AGENT_PID`` so both VCS and build commands can
        authenticate for the whole build.

        No-op for public/HTTPS repos, or when private repos aren't allowed.
        """
        if not self.data.allow_private_repos:
            return

        repo_url = self.data.project.repo or ""
        if not (repo_url.startswith("git@") or repo_url.startswith("ssh://")):
            return

        private_key = get_project_ssh_key(self.data.api_client, self.data.project.pk)
        if not private_key:
            log.warning("SSH repository has no deploy key; skipping ssh-agent setup.")
            return

        key_path = self._write_ssh_key(private_key)
        try:
            agent = self.vcs_environment.run("ssh-agent", "-s", record=False)
            agent_env = parse_ssh_agent_env(agent.output)
            if not agent_env.get("SSH_AUTH_SOCK"):
                log.warning("ssh-agent did not report SSH_AUTH_SOCK.", output=agent.output)
                return

            self.ssh_agent_env = {
                name: agent_env[name]
                for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")
                if name in agent_env
            }
            # Make the agent reachable by the VCS commands about to run; build
            # commands pick it up later via ``get_build_env_vars``.
            self.vcs_environment._environment.update(self.ssh_agent_env)

            # ``-t <ttl>``: expire the identity as a backstop if teardown is
            # missed. The TTL outlasts the build's time limit so it never
            # expires mid-build.
            self.vcs_environment.run(
                "ssh-add", "-t", str(self._ssh_key_ttl()), key_path, record=False
            )
        finally:
            # The agent holds the key now; the file is no longer needed.
            os.unlink(key_path)

    def _write_ssh_key(self, private_key: str) -> str:
        """
        Write the private key where BOTH sides can reach it.

        ``ssh-add`` runs inside the build container, but the runner writes the
        file here on the host — so it has to land under the docroot, which is
        bind-mounted into the container at the same path.
        """
        key_dir = Path(self.data.project.doc_path) / "checkouts"
        assert_path_is_inside_docroot(key_dir)
        self.vcs_environment.run(
            "mkdir",
            "--parents",
            str(key_dir),
            cwd="/",
            record=False,
            # The checkout goes in here next.
            warn_only=False,
        )

        key_path = key_dir / f"{self.data.version.slug}-key"
        assert_path_is_inside_docroot(key_path)

        # Create it 0600 from the start: never let the key exist group- or
        # world-readable, even briefly, on a path the build container can see.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as key_file:
            key_file.write(private_key)
        os.chmod(key_path, 0o400)

        uid, gid = _build_user_ids()
        try:
            os.chown(key_path, uid, gid)
        except OSError:
            # Already ours (production, where the runner *is* the build user).
            log.debug("Could not chown SSH key to the build user.", uid=uid, gid=gid)
        return str(key_path)

    def _ssh_key_ttl(self) -> int:
        """TTL (seconds) for the loaded identity — outlasts the build limit."""
        try:
            limit = int(os.environ.get("RTD_BUILD_TIME_LIMIT_SECONDS", "0"))
        except ValueError:
            limit = 0
        # Fall back to 4h when the worker didn't inject a limit; add a buffer so
        # the key never expires during a legitimately slow build.
        return (limit or 4 * 60 * 60) + 15 * 60

    def get_vcs_env_vars(self):
        """Env vars used during VCS commands (clone/fetch/checkout)."""
        env = self.get_rtd_env_vars()
        # Don't prompt for username (Git 2.3+).
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["READTHEDOCS_GIT_CLONE_TOKEN"] = self.data.project.clone_token
        self._add_git_ssh_command_env_var(env)
        return env

    def get_rtd_env_vars(self):
        """The ``READTHEDOCS_*`` env vars exposed to every build command."""
        env = {
            "READTHEDOCS": "True",
            "READTHEDOCS_VERSION": self.data.version.slug,
            "READTHEDOCS_VERSION_TYPE": self.data.version.type,
            "READTHEDOCS_VERSION_NAME": self.data.version.verbose_name,
            "READTHEDOCS_PROJECT": self.data.project.slug,
            "READTHEDOCS_LANGUAGE": self.data.project.language,
            "READTHEDOCS_REPOSITORY_PATH": self.data.project.checkout_path(self.data.version.slug),
            "READTHEDOCS_OUTPUT": os.path.join(
                self.data.project.checkout_path(self.data.version.slug), "_readthedocs/"
            ),
            "READTHEDOCS_GIT_CLONE_URL": self.data.project.repo,
            "READTHEDOCS_GIT_IDENTIFIER": self.data.version.git_identifier,
            "READTHEDOCS_GIT_COMMIT_HASH": self.data.build.get("commit") or "",
            "READTHEDOCS_PRODUCTION_DOMAIN": self.data.production_domain,
        }
        return env

    def get_build_env_vars(self):
        """Env vars used during the actual build (post-VCS)."""
        env = self.get_rtd_env_vars()
        env["NO_COLOR"] = "1"  # https://no-color.org/

        if self.data.config.conda is not None:
            env.update(
                {
                    "CONDA_ENVS_PATH": os.path.join(self.data.project.doc_path, "conda"),
                    "CONDA_DEFAULT_ENV": self.data.version.slug,
                    "BIN_PATH": os.path.join(
                        self.data.project.doc_path,
                        "conda",
                        self.data.version.slug,
                        "bin",
                    ),
                }
            )
        else:
            env.update(
                {
                    "BIN_PATH": os.path.join(
                        self.data.project.doc_path,
                        "envs",
                        self.data.version.slug,
                        "bin",
                    ),
                }
            )

        # UV_PROJECT_ENVIRONMENT and READTHEDOCS_VIRTUALENV_PATH are the same
        venv_path = os.path.join(
            self.data.project.doc_path,
            "envs",
            self.data.version.slug,
        )
        if self.data.config.is_using_uv:
            checkout_path = self.data.project.checkout_path(self.data.version.slug)
            try:
                # UV_PROJECT has to be an absolute path because we run `uv venv` and `uv run`
                # from different `cwd` directories.
                UV_PROJECT = os.path.join(checkout_path, self.data.config.python.install[0].path)
            except AttributeError, IndexError, TypeError:
                UV_PROJECT = checkout_path

            env.update(
                {
                    # Point UV_PYTHON at the Python inside the virtualenv uv
                    # creates. Used by ``uv run``/``uv sync``, but also by
                    # ``uv pip install`` to pick the right Python — pointing it
                    # at the asdf one installs packages system-wide instead.
                    "UV_PYTHON": os.path.join(venv_path, "bin", "python"),
                    "UV_PROJECT": UV_PROJECT,
                    "UV_PROJECT_ENVIRONMENT": venv_path,
                }
            )

        env.update(
            {
                "READTHEDOCS_CANONICAL_URL": self.data.version.canonical_url,
                "READTHEDOCS_VIRTUALENV_PATH": venv_path,
            }
        )

        self._add_git_ssh_command_env_var(env)

        # Keep the deploy-key agent available to build commands too (e.g. user
        # jobs that fetch git submodules), matching the old whole-build behavior.
        env.update(self.ssh_agent_env)

        # Project-level env vars; private values are stripped on PR builds.
        env.update(
            self.data.project.environment_variables(public_only=self.data.version.is_external)
        )

        return env

    def is_type_sphinx(self):
        """True if the resolved doctype is one of the Sphinx variants."""
        return "sphinx" in self.data.config.doctype

    def store_readthedocs_build_yaml(self):
        """
        Capture the user's optional ``readthedocs-build.yaml`` for the API.

        The file is dropped into the HTML output dir by post-build user
        commands; we slurp it and stash it on the version so the
        ``/_/readthedocs-config.json`` endpoint can serve it.
        """
        yaml_path = os.path.join(
            self.data.project.artifact_path(version=self.data.version.slug, type_="html"),
            "readthedocs-build.yaml",
        )

        if not os.path.exists(yaml_path):
            log.debug("Build output YAML file (readthedocs-build.yaml) does not exist.")
            return

        try:
            import yaml

            with safe_open(yaml_path, "r") as f:
                data = yaml.safe_load(f)
        except Exception:
            # Mirror upstream: skip silently for now until the schema is finalized.
            return

        log.info("readthedocs-build.yaml loaded.", path=yaml_path)
        self.data.version.build_data = data

    def attach_notification(
        self,
        attached_to,
        message_id,
        format_values=None,
        state="unread",
        dismissable=False,
        news=False,
    ):
        """
        POST a notification to the API.

        :param attached_to: ``project/<id>`` or ``build/<id>``.
        """
        format_values = format_values or {}
        self.data.api_client.notifications.post(
            {
                "attached_to": attached_to,
                "message_id": message_id,
                "state": state,
                "dismissable": dismissable,
                "news": news,
                "format_values": format_values,
            }
        )

    def download_artifacts_from_storage(self):
        """
        Download the build artifacts from storage.

        This is used when the build is uploaded to storage and we want to
        download it to the build environment.
        """
        from builder.storage import StorageType
        from builder.storage import get_storage

        build_artifacts_storage = get_storage(
            build_id=self.data.build["id"],
            api_client=self.data.api_client,
            storage_type=StorageType.build_uploads,
            endpoint_url=self.data.s3_endpoint_url,
        )

        destination = (
            Path(self.data.project.checkout_path(self.data.version.slug)) / "artifacts.zip"
        )
        self.build_environment.run(
            "mkdir",
            "-p",
            str(destination.parent),
            record=False,
            # The download writes into this directory from the host.
            warn_only=False,
        )
        storage_path = self.data.build["uploaded_artifacts_storage_path"]

        log.info("Downloading build artifacts from storage.", storage_path=storage_path)
        build_artifacts_storage.download_to_path(storage_path, destination)

    def extract_artifacts(self):
        """
        Extract the build artifacts to the destination.

        This is used when the build is uploaded to storage and we want to
        extract it to the build environment.
        """
        # Use READTHEDOCS_ variables to keep consistency with the rest of the build commands recorded
        source = "$READTHEDOCS_REPOSITORY_PATH/artifacts.zip"
        destination = "$READTHEDOCS_OUTPUT"

        log.info("Extracting build artifacts.")
        self.build_environment.run(
            "mkdir",
            "-p",
            destination,
            record=False,
        )
        result = self.build_environment.run(
            "unzip",
            source,
            "-d",
            destination,
            # Keep the command recorded (it's useful in the build UI) but let a
            # non-zero exit fall through to the specific notification below
            # instead of the generic one ``run`` raises.
            warn_only=True,
        )

        if result.exit_code != 0:
            log.info(
                "Error extracting artifacts.",
                exit_code=result.exit_code,
                output=result.output,
            )
            raise BuildUserError(BuildUserError.BUILD_ARTIFACTS_ZIP_INVALID)

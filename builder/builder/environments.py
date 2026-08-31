"""
Build command and build environment.

Ported from ``readthedocs.doc_builder.environments``, keeping upstream's
split between a local and a Docker environment.

The runner executes on the *host* and runs every build command inside the
build container via ``docker exec``, the same way upstream's
``DockerBuildCommand`` does. The container itself is started and stopped by
the worker; this module only execs into it by name, which it reads from
``build_env.container_name``.

Privilege drop is ``docker exec --user`` — there is no ``runuser``
wrapping, and no user database lookup, because the user is resolved by
Docker inside the container rather than by us on the host.

:class:`BuildEnvironment` / :class:`BuildCommand` run commands as plain
subprocesses and exist **for tests only** — see the warning on that class.
Production uses :class:`DockerBuildEnvironment` / :class:`DockerBuildCommand`.
"""

import datetime
import os
import re
import subprocess

import structlog
from docker.errors import APIError as DockerAPIError
from slumber.exceptions import HttpNotFoundError

from builder import settings
from builder.constants import DATA_UPLOAD_MAX_OUTPUT_BYTES
from builder.constants import RTD_SKIP_BUILD_EXIT_CODE
from builder.exceptions import BuildAppError
from builder.exceptions import BuildCancelled
from builder.exceptions import BuildUserError


log = structlog.get_logger(__name__)


def _truncate_output(output: str | None) -> str:
    """Trim ``output`` to head + tail with an elision marker (used for log lines)."""
    if output is None:
        return ""
    output_lines = output.split("\n")
    if len(output_lines) <= 20:
        return output
    return "\n".join(output_lines[:10] + [" ..Output Truncated.. "] + output_lines[-10:])


# Exit codes signalling the command was killed for exceeding memory. SIGKILL
# (9) is what the OOM killer and ``--memory`` enforcement send: ``docker exec``
# reports that as ``128+N`` = 137 (upstream's ``DOCKER_OOM_EXIT_CODE``), while
# ``subprocess`` on the local path reports it as ``-N``.
_OOM_EXIT_CODES = frozenset({137, -9})


# Matches ``$NAME`` and ``${NAME}`` env-var references — the same forms shells
# expand. Deliberately *doesn't* match ``$$``, ``$(...)``, ``$1``, etc., so
# user-supplied shell expressions pass through unchanged. Only used by the
# local (test-only) execution path; under ``docker exec`` the container's
# shell does the expansion.
_ENV_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(value, env: dict) -> str:
    """
    Expand ``$NAME`` / ``${NAME}`` references in ``value`` from ``env``.

    Returns ``value`` unchanged if it isn't a string. Unknown variables are
    left as-is (matching ``string.Template.safe_substitute`` semantics) so
    typos surface as runtime errors instead of empty strings.
    """
    if not isinstance(value, str):
        return value

    def _repl(m):
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))

    return _ENV_VAR_RE.sub(_repl, value)


def _killed_by_oom(exit_code, output) -> bool:
    """Heuristic: was the command killed for exceeding the memory limit?"""
    if exit_code in _OOM_EXIT_CODES:
        return True
    # The kernel doesn't always surface a specific code; fall back to the
    # "Killed" line it prints. Restricted to exit 1 like upstream so unrelated
    # failures whose output merely mentions "Killed" aren't misclassified.
    if exit_code == 1:
        tail = "\n".join((output or "").splitlines()[-15:])
        return "Killed" in tail
    return False


class BuildCommandResultMixin:
    """
    Common command-result properties.

    Ported verbatim from ``readthedocs.builds.models.BuildCommandResultMixin``.
    """

    @property
    def successful(self) -> bool:
        return self.exit_code == 0

    @property
    def failed(self) -> bool:
        return not self.successful

    @property
    def finished(self) -> bool:
        return self.end_time is not None


class BuildCommand(BuildCommandResultMixin):
    """
    Wrap a single command's execution.

    Acts as a mapping to the API representation of
    ``BuildCommandResult``. The ``save()`` method posts (or patches) the
    command record via the API client.

    :param command: sequence of command parts. A single-element sequence
        holding a shell expression is the ``escape_command=False`` form.
    :param cwd: absolute working directory, defaults to ``RTD_DOCKER_WORKDIR``.
    :param environment: dict of additional env vars (``PATH`` is rejected;
        use ``bin_path`` instead).
    :param user: target user for the command. Applied by
        :class:`DockerBuildCommand` via ``docker exec --user``; ignored on the
        local path, which always runs as the current user.
    :param build_env: owning :class:`BuildEnvironment`.
    :param bin_path: directory prepended to ``PATH`` for resolution.
    :param record_as_success: force ``exit_code`` to ``0`` when recorded.
    :param demux: capture stderr separately from stdout.
    :param escape_command: when ``False``, the command parts are passed to the
        container's shell unescaped (used for user-supplied ``build.jobs`` and
        ``build.commands``, which are meant to be shell expressions).
        Defaults to ``True``.
    """

    def __init__(
        self,
        command,
        cwd=None,
        environment=None,
        user=None,
        build_env=None,
        bin_path=None,
        record_as_success=False,
        demux=False,
        escape_command=True,
        **kwargs,
    ):
        self.id = None
        self.command = command
        self.escape_command = escape_command
        self.shell = not escape_command
        # ``command`` is iterated part-by-part; a bare string would silently
        # char-split (``"true"`` -> ``"t r u e"``). Callers go through
        # ``BuildEnvironment.run(*cmd)``, which always produces a tuple, so
        # this is caller misuse.
        if isinstance(command, str):
            raise TypeError("BuildCommand needs a sequence of command parts, not a string.")
        self.cwd = cwd or settings.RTD_DOCKER_WORKDIR
        self.user = user or settings.RTD_DOCKER_USER
        self._environment = environment.copy() if environment else {}
        if "PATH" in self._environment:
            raise BuildAppError(
                BuildAppError.GENERIC_WITH_BUILD_ID,
                exception_message="'PATH' can't be set. Use bin_path",
            )

        self.build_env = build_env
        self.output = None
        self.error = None
        self.start_time = None
        self.end_time = None

        self.bin_path = bin_path
        self.record_as_success = record_as_success
        self.demux = demux
        self.exit_code = None

        if self.build_env:
            if self.build_env.project and self.build_env.version:
                structlog.contextvars.bind_contextvars(
                    project_slug=self.build_env.project.slug,
                    version_slug=self.build_env.version.slug,
                )
            if self.build_env.build:
                structlog.contextvars.bind_contextvars(
                    build_id=self.build_env.build.get("id"),
                )

    def __str__(self):
        output = ""
        if self.output is not None:
            output = self.output.encode("utf-8")
        return "\n".join([self.get_command(), str(output)])

    def get_command(self) -> str:
        """Flatten ``command`` to a single shell-safe display string."""
        if hasattr(self.command, "__iter__") and not isinstance(self.command, str):
            return " ".join(self.command)
        return self.command

    def run(self):
        """
        Execute the command as a plain subprocess on this host.

        Only used by :class:`BuildEnvironment` — see the note there. The
        production path is :meth:`DockerBuildCommand.run`.
        """
        self.start_time = datetime.datetime.now(datetime.timezone.utc)
        environment = self._environment.copy()
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        environment.pop("PYTHONPATH", None)
        environment = {k: v for k, v in environment.items() if v is not None}

        env_paths = os.environ.get("PATH", "").split(":")
        if self.bin_path is not None:
            env_paths.insert(0, self.bin_path)
        environment["PATH"] = ":".join(env_paths)

        if self.shell:
            spawn_cmd = " ".join(self.command)
        else:
            # ``subprocess`` does not expand ``$VAR``; the container's shell
            # does that for us on the Docker path.
            template_env = {**os.environ, **environment}
            spawn_cmd = [_expand_env_vars(arg, template_env) for arg in self.command]

        log.info("Running build command.", command=self.get_command(), cwd=self.cwd)

        try:
            stderr = subprocess.PIPE if self.demux else subprocess.STDOUT
            proc = subprocess.Popen(
                spawn_cmd,
                shell=self.shell,
                cwd=self.cwd,
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=environment,
            )
            cmd_stdout, cmd_stderr = proc.communicate()
            self.output = self.decode_output(cmd_stdout)
            self.error = self.decode_output(cmd_stderr)
            self.exit_code = proc.returncode
            if _killed_by_oom(self.exit_code, self.output):
                self.output += "\n\nCommand killed due to excessive memory consumption\n"
        except OSError:
            log.exception("Operating system error.")
            self.exit_code = -1
        finally:
            self.end_time = datetime.datetime.now(datetime.timezone.utc)

    def decode_output(self, output: bytes) -> str:
        """Decode bytes to a UTF-8 string with replacement for invalid sequences."""
        decoded = ""
        try:
            decoded = output.decode("utf-8", "replace")
        except TypeError, AttributeError:
            pass
        return decoded

    def sanitize_output(self, output: str) -> str:
        r"""
        Sanitize ``output`` before sending to the API.

        - Strips NUL bytes (PostgreSQL won't accept them in text columns).
        - Truncates to ``DATA_UPLOAD_MAX_OUTPUT_BYTES`` to fit a single API request.
        - Obfuscates values of private project environment variables.
        """
        sanitized = ""
        try:
            sanitized = output.replace("\x00", "")
        except TypeError, AttributeError:
            pass

        # Truncate when the encoded output exceeds the API request budget.
        output_length = len(sanitized.encode("utf-8"))
        allowed_length = DATA_UPLOAD_MAX_OUTPUT_BYTES
        if output_length > allowed_length:
            log.info("Command output is too big.", command=self.get_command())
            truncated_output = sanitized[-allowed_length:]
            sanitized = (
                ".. (truncated) ...\n"
                f"Output is too big. Truncated at {allowed_length} bytes.\n\n\n"
                f"{truncated_output}"
            )

        # Obfuscate private environment variable values.
        if self.build_env and self.build_env.project:
            env_vars = getattr(self.build_env.project, "_environment_variables", {}) or {}
            for name, spec in env_vars.items():
                if not spec.get("public"):
                    value = spec["value"]
                    obfuscated_value = f"{value[:4]}****"
                    sanitized = sanitized.replace(value, obfuscated_value)

        return sanitized

    def save(self, api_client):
        """
        Save (or update) this command via the RTD API.

        First call POSTs and remembers the returned id; subsequent calls
        PATCH the existing record. If the PATCH 404s (build was restarted
        upstream), we fall back to POSTing a fresh record.

        Datetimes are serialized to ISO 8601 strings since the slumber
        client uses the stdlib JSON encoder (which can't handle
        ``datetime`` directly). Upstream relies on DRF's JSONRenderer for
        the same purpose.
        """
        if self.record_as_success and self.exit_code is not None:
            log.warning("Recording command exit_code as success")
            self.exit_code = 0

        data = {
            "build": self.build_env.build.get("id"),
            "command": self.get_command(),
            "output": self.sanitize_output(self.output),
            "exit_code": self.exit_code,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
        }

        if self.id:
            try:
                resp = api_client.command(self.id).patch(data)
            except HttpNotFoundError:
                log.exception("Build command has an id but doesn't exist in the database.")
                resp = api_client.command.post(data)
        else:
            resp = api_client.command.post(data)

        log.debug("Response via JSON encoded data.", response=resp)
        self.id = resp.get("id")


class DockerBuildCommand(BuildCommand):
    """
    A :class:`BuildCommand` executed inside the build container via ``docker exec``.

    Ported from upstream's class of the same name. The container is already
    running — the worker started it — so this only ever execs into it.
    """

    bash_escape_re = re.compile(
        r"([\s\!\"\#\$\&\'\(\)\*\:\;\<\>\?\@\[\\\]\^\`\{\|\}\~])"  # noqa
    )

    def _escape_command(self, cmd: str) -> str:
        r"""Escape the command by prefixing suspicious chars with ``\``."""
        command = self.bash_escape_re.sub(r"\\\1", cmd)

        # Variables we deliberately want the container's shell to expand, so
        # they must survive escaping. Ported verbatim from upstream.
        # Every variable a command of ours actually references. The container's
        # shell has to expand these, so they must survive escaping — anything
        # missing here arrives at the command as a literal ``$NAME`` string.
        # Keep in step with the vars ``BuildDirector.get_rtd_env_vars`` sets.
        not_escape_variables = (
            "READTHEDOCS_OUTPUT",
            "READTHEDOCS_REPOSITORY_PATH",
            "READTHEDOCS_VIRTUALENV_PATH",
            "READTHEDOCS_GIT_CLONE_TOKEN",
            "CONDA_ENVS_PATH",
            "CONDA_DEFAULT_ENV",
        )
        for variable in not_escape_variables:
            command = command.replace(f"\\${variable}", f"${variable}")
        return command

    def get_wrapped_command(self) -> str:
        """
        Wrap the command in a shell, optionally escaping special bash chars.

        ``bin_path`` is prepended to the *container's* ``$PATH`` rather than
        replacing it — the runner's own PATH is a host path and means nothing
        inside the container.

        ``nice`` keeps a user's build command from starving the container's
        other work, matching upstream.
        """
        prefix = ""
        if self.bin_path:
            bin_path = self._escape_command(self.bin_path)
            prefix += f"PATH={bin_path}:$PATH "

        command = " ".join(
            self._escape_command(part) if self.escape_command else part for part in self.command
        )

        nice = "nice -n 10"
        if prefix:
            # ``;`` separates the variable assignment from the command as
            # explicitly as a newline would.
            return f"{nice} /bin/sh -c '{prefix}; {command}'"
        return f"{nice} /bin/sh -c '{command}'"

    def run(self):
        """Execute the command inside the build container and capture output."""
        self.start_time = datetime.datetime.now(datetime.timezone.utc)
        environment = self._environment.copy()
        # Strip Django bootstrap noise that may leak from a parent shell.
        environment.pop("DJANGO_SETTINGS_MODULE", None)
        environment.pop("PYTHONPATH", None)

        # Drop None values: the Docker API rejects them. Common culprits:
        # clone_token / canonical_url / git_identifier on public repos and
        # machine-created versions.
        environment = {k: v for k, v in environment.items() if v is not None}

        # PATH is handled in ``get_wrapped_command`` (prepended to the
        # container's own), and HOME/USER come from the container's passwd
        # entry via ``--user``. Neither is set here.

        container = self.build_env.container_name
        if not container:
            raise BuildAppError(
                BuildAppError.GENERIC_WITH_BUILD_ID,
            )

        log.info(
            "Running build command in container.",
            container=container,
            command=self.get_command(),
            cwd=self.cwd,
            user=self.user,
        )

        client = self.build_env.client
        try:
            exec_cmd = client.exec_create(
                container=container,
                cmd=self.get_wrapped_command(),
                environment=environment,
                user=self.user,
                workdir=self.cwd,
                stdout=True,
                stderr=True,
            )
            out = client.exec_start(exec_id=exec_cmd["Id"], stream=False, demux=self.demux)

            cmd_stdout, cmd_stderr = out if self.demux else (out, b"")
            self.output = self.decode_output(cmd_stdout)
            self.error = self.decode_output(cmd_stderr)
            self.exit_code = client.exec_inspect(exec_id=exec_cmd["Id"])["ExitCode"]

            # Surface an OOM kill in the recorded output; the specific
            # ``BUILD_EXCESSIVE_MEMORY`` notification is raised by the caller.
            if _killed_by_oom(self.exit_code, self.output):
                self.output += "\n\nCommand killed due to excessive memory consumption\n"
        except DockerAPIError:
            log.exception("Docker API error running build command.")
            self.exit_code = -1
            if not self.output:
                self.output = "Command exited abnormally"
        finally:
            self.end_time = datetime.datetime.now(datetime.timezone.utc)


class BuildEnvironment:
    """
    Build environment that runs commands as plain subprocesses on this host.

    .. warning::

       **This class is for tests only. Production always uses
       :class:`DockerBuildEnvironment`.**

       It exists because a chunk of the suite — ``tests/test_vcs.py`` and the
       LaTeX exit-code tests in ``tests/test_backends.py`` — drives real
       ``git`` and shell commands against temporary directories to assert on
       their actual behaviour. Running those through ``docker exec`` would
       require a Docker daemon in CI and make the suite far slower, for no
       extra coverage of the code under test (the VCS backends and the
       exit-code promotion logic don't care how the command was spawned).

       Upstream keeps the same split — ``LocalBuildEnvironment`` and
       ``DockerBuildEnvironment`` — so this is parity, not a shortcut. Nothing
       in the runner may instantiate this class: the director and runner build
       :class:`DockerBuildEnvironment` explicitly, so user code can never be
       executed outside the build container.

    Owns a list of executed commands and an API client used to record them.

    :param project: APIProject backing this build (or ``None`` for
        sync-repo-style invocations).
    :param version: APIVersion backing this build.
    :param build: build dict from the API.
    :param config: parsed config.
    :param environment: base environment variables passed to every command.
    :param record: when ``True``, each executed command is POSTed to the API.
    :param api_client: slumber API client; required when ``record=True``.
    :param allow_private_repos: whether this Read the Docs supports private
        repositories. Read by the VCS backends to pick a clone-error message.
    """

    command_class = BuildCommand

    def __init__(
        self,
        project=None,
        version=None,
        build=None,
        config=None,
        environment=None,
        record=True,
        api_client=None,
        allow_private_repos=False,
        **kwargs,
    ):
        self.allow_private_repos = allow_private_repos
        self.project = project
        self.version = version
        self.build = build
        self.config = config
        self._environment = environment or {}
        self.commands = []
        self.record = record
        self.api_client = api_client

        if self.record and not self.api_client:
            raise ValueError("api_client is required when record=True")

    def record_command(self, command: BuildCommand):
        if self.record:
            command.save(self.api_client)

    def run(self, *cmd, **kwargs) -> BuildCommand:
        """
        Run a command using this environment's :attr:`command_class`.

        See :meth:`run_command_class` for the full kwarg list.
        """
        return self.run_command_class(self.command_class, cmd, **kwargs)

    def run_command_class(self, cls, cmd, **kwargs) -> BuildCommand:
        """
        Run a command using ``cls`` (a :class:`BuildCommand` subclass).

        Used by the PDF builder to substitute a LaTeX-aware subclass that
        treats some non-zero exit codes as success. Otherwise identical to
        :meth:`run`.

        :param warn_only: don't raise on non-zero exit (default ``False``).
        :param record: when ``False``, also implies ``warn_only=True``.
        :param record_as_success: force the recorded ``exit_code`` to ``0``.
        :param escape_command: see :class:`BuildCommand`.

        Other kwargs are forwarded to ``cls``.
        """
        record = kwargs.pop("record", True)
        # An unrecorded command has nowhere to report a failure, so by default
        # it only warns. Callers can still opt back in with an explicit
        # ``warn_only=False`` — worth doing for anything the build genuinely
        # depends on, like creating the checkout directory: a swallowed
        # ``mkdir`` surfaces much later as a command dying on a missing cwd.
        warn_only = kwargs.pop("warn_only", not record)
        record_as_success = kwargs.pop("record_as_success", False)

        if record_as_success:
            record = True
            warn_only = True
            kwargs["record_as_success"] = record_as_success

        # Pass the env vars through, swapping ``BIN_PATH`` for ``bin_path``
        # so the parent shell's PATH is left intact.
        environment = self._environment.copy()
        env_path = environment.pop("BIN_PATH", None)
        if "bin_path" not in kwargs and env_path:
            kwargs["bin_path"] = env_path
        if "environment" in kwargs:
            raise BuildAppError(
                BuildAppError.GENERIC_WITH_BUILD_ID,
                exception_message="environment can't be passed in via commands.",
            )
        kwargs["environment"] = environment
        kwargs["build_env"] = self
        build_cmd = cls(cmd, **kwargs)

        if record:
            self.record_command(build_cmd)
            self.commands.append(build_cmd)

        build_cmd.run()

        if record:
            self.record_command(build_cmd)

        if build_cmd.failed:
            if warn_only:
                log.warning(
                    "Command failed",
                    command=build_cmd.get_command(),
                    output=_truncate_output(build_cmd.output),
                    stderr=_truncate_output(build_cmd.error),
                    exit_code=build_cmd.exit_code,
                    project_slug=self.project.slug if self.project else "",
                    version_slug=self.version.slug if self.version else "",
                )
            elif build_cmd.exit_code == RTD_SKIP_BUILD_EXIT_CODE:
                raise BuildCancelled(BuildCancelled.SKIPPED_EXIT_CODE_183)
            elif _killed_by_oom(build_cmd.exit_code, build_cmd.output):
                raise BuildUserError(BuildUserError.BUILD_EXCESSIVE_MEMORY)
            else:
                # Surface the failed command's output before bubbling up.
                # In production the API recording captures it for the build
                # web UI, but with ``record=False`` (dev runs) the output
                # would otherwise be lost entirely.
                log.error(
                    "Build command failed",
                    command=build_cmd.get_command(),
                    exit_code=build_cmd.exit_code,
                    output=_truncate_output(build_cmd.output),
                    stderr=_truncate_output(build_cmd.error),
                )
                raise BuildUserError(BuildUserError.GENERIC)
        return build_cmd


class DockerBuildEnvironment(BuildEnvironment):
    """
    The production build environment: every command runs via ``docker exec``.

    The container's lifecycle belongs to the worker, which starts it before
    spawning the runner and stops it afterwards. This class only execs into
    it by name, and never creates or removes it.

    :param container_name: name of the running build container, passed down
        from the worker that started it.
    :param docker_client: the client the worker used to start it. Shared
        rather than rebuilt, so one build means one connection to the daemon.
    """

    command_class = DockerBuildCommand

    def __init__(self, *args, container_name=None, docker_client=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.container_name = container_name
        self.client = docker_client

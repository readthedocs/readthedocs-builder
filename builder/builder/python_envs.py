"""
Python (language) environments.

Ported from ``readthedocs.doc_builder.python_environments``. Three flavours,
selected by ``BuildDirector.setup_environment`` based on the user's config:

- :class:`Virtualenv` — default, ``python -mvirtualenv``.
- :class:`UvEnv` — ``uv venv`` plus ``uv sync`` / ``uv pip install`` for installs.
- :class:`Conda` — ``conda env create`` (or ``mamba``) from a user-supplied
  ``environment.yml``.

Each subclass overrides :meth:`setup_base`, :meth:`install_core_requirements`,
:meth:`install_requirements_file`, and (where applicable) :meth:`install_uv`.

De-Django changes from upstream:

- ``readthedocs.config`` imports → :mod:`builder.config`.
- ``readthedocs.projects.models.Feature`` → :class:`builder.constants.Feature`.
- ``readthedocs.doc_builder.config.load_yaml_config`` fallback for when no
  ``config`` is passed is dropped: the runner always passes the parsed config.
"""

import copy
import os

import structlog
import yaml

from builder.config import PIP
from builder.config import SETUPTOOLS
from builder.config import ParseError
from builder.config import parse as parse_yaml
from builder.config.models import PythonInstall
from builder.config.models import PythonInstallRequirements
from builder.config.models import UvInstall
from builder.constants import GENERIC
from builder.constants import Feature
from builder.exceptions import UserFileNotFound
from builder.filesystem import safe_open


log = structlog.get_logger(__name__)


class PythonEnvironment:
    """An isolated environment into which Python packages can be installed."""

    def __init__(self, version, build_env, config=None):
        self.version = version
        self.project = version.project
        self.build_env = build_env
        if config is None:
            # Upstream falls back to ``load_yaml_config(version)`` when no
            # config is passed; the runner always supplies one, so we raise
            # rather than carry that machinery.
            raise ValueError("PythonEnvironment requires a parsed config object")
        self.config = config
        self.checkout_path = self.project.checkout_path(self.version.slug)
        structlog.contextvars.bind_contextvars(
            project_slug=self.project.slug,
            version_slug=self.version.slug,
        )

    def install_requirements(self):
        """Install every entry in ``python.install`` according to its type."""
        for install in self.config.python.install:
            if isinstance(install, PythonInstallRequirements):
                self.install_requirements_file(install)
            elif isinstance(install, PythonInstall):
                self.install_package(install)
            elif isinstance(install, UvInstall):
                self.install_uv(install)

    def install_uv(self, install):
        """Install via ``uv``. Subclasses must override; default raises."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support uv installs")

    def install_package(self, install):
        """
        Install a path-based package via ``pip`` or ``setuptools``.

        :param install: a :class:`PythonInstall` from the parsed config.
        """
        if install.method == PIP:
            # Prefix ``./`` so pip installs from a local path rather than PyPI.
            local_path = os.path.join(".", install.path) if install.path != "." else install.path
            extra_req_param = ""
            if install.extra_requirements:
                extra_req_param = "[{}]".format(",".join(install.extra_requirements))
            self.build_env.run(
                self.venv_bin(filename="python"),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--upgrade-strategy",
                "only-if-needed",
                "--no-cache-dir",
                f"{local_path}{extra_req_param}",
                cwd=self.checkout_path,
                bin_path=self.venv_bin(),
            )
        elif install.method == SETUPTOOLS:
            self.build_env.run(
                self.venv_bin(filename="python"),
                os.path.join(install.path, "setup.py"),
                "install",
                "--force",
                cwd=self.checkout_path,
                bin_path=self.venv_bin(),
            )

    def venv_bin(self, prefixes, filename=None):
        """
        Build a path inside the virtualenv's ``bin`` directory.

        Subclasses pass a ``prefixes`` list anchored at the venv root (or its
        env-var placeholder); ``filename``, if given, is appended.
        """
        if filename is not None:
            prefixes.append(filename)
        return os.path.join(*prefixes)


class Virtualenv(PythonEnvironment):
    """A standard ``virtualenv`` environment (the default)."""

    def venv_bin(self, filename=None):
        prefixes = ["$READTHEDOCS_VIRTUALENV_PATH", "bin"]
        return super().venv_bin(prefixes, filename=filename)

    def setup_base(self):
        """Create the venv with ``python -mvirtualenv``."""
        cli_args = [
            "-mvirtualenv",
            # Positional destination argument.
            "$READTHEDOCS_VIRTUALENV_PATH",
        ]

        self.build_env.run(
            self.config.python_interpreter,
            *cli_args,
            # The venv bin doesn't exist yet.
            bin_path=None,
            # Project root may have config files that interfere.
            cwd=None,
        )

    def install_core_requirements(self):
        """Install RTD's baseline pip + sphinx/mkdocs into the venv."""
        pip_install_cmd = [
            self.venv_bin(filename="python"),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--no-cache-dir",
        ]
        self._install_latest_requirements(pip_install_cmd)

    def _install_latest_requirements(self, pip_install_cmd):
        # Step 1: upgrade pip + setuptools to current.
        cmd = pip_install_cmd + ["pip", "setuptools"]
        self.build_env.run(
            *cmd,
            bin_path=self.venv_bin(),
            cwd=self.checkout_path,
        )

        # Generic builds bring their own deps; nothing else to install.
        if self.config.doctype == GENERIC:
            return

        # Step 2: install the doctype's runtime (sphinx or mkdocs).
        requirements = []
        if self.config.doctype == "mkdocs":
            requirements.append("mkdocs")
        else:
            requirements.append("sphinx")

        cmd = copy.copy(pip_install_cmd)
        cmd.extend(requirements)
        self.build_env.run(
            *cmd,
            bin_path=self.venv_bin(),
            cwd=self.checkout_path,
        )

    def install_requirements_file(self, install):
        """
        Install a user-provided requirements file via ``pip``.

        :param install: a :class:`PythonInstallRequirements` from the config.
        """
        requirements_file_path = install.requirements
        if requirements_file_path:
            args = [
                self.venv_bin(filename="python"),
                "-m",
                "pip",
                "install",
            ]
            if self.project.has_feature(Feature.PIP_ALWAYS_UPGRADE):
                args += ["--upgrade"]
            args += [
                "--exists-action=w",
                "--no-cache-dir",
                "-r",
                requirements_file_path,
            ]
            self.build_env.run(
                *args,
                cwd=self.checkout_path,
                bin_path=self.venv_bin(),
            )


class UvEnv(Virtualenv):
    """A ``uv``-managed virtual environment."""

    def setup_base(self):
        """Create the env with ``uv venv``."""
        # UV_PYTHON points at the Python *inside* the venv, which doesn't exist
        # yet — drop it for this command and put it back afterwards.
        uv_python = self.build_env._environment.pop("UV_PYTHON", None)
        self.build_env.run(
            "uv",
            "venv",
            "$READTHEDOCS_VIRTUALENV_PATH",
            bin_path=None,
            cwd=None,
        )
        self.build_env._environment["UV_PYTHON"] = uv_python

    def install_core_requirements(self):
        """uv-managed envs skip the pip/sphinx core bootstrap."""

    def install_uv(self, install):
        """
        Dispatch ``uv sync`` vs ``uv pip install`` based on ``install.command``.

        :param install: a :class:`UvInstall` from the config.
        """
        if install.command == "sync":
            self._install_uv_sync(install)
        elif install.command == "pip":
            self._install_uv_pip(install)

    def _install_uv_sync(self, install):
        args = ["uv", "sync"]

        if install.groups:
            if install.groups == "all":
                args.append("--all-groups")
            else:
                for group in install.groups:
                    args.extend(["--group", group])

        if install.extras:
            if install.extras == "all":
                args.append("--all-extras")
            else:
                for extra in install.extras:
                    args.extend(["--extra", extra])

        self.build_env.run(
            *args,
            cwd=self.checkout_path,
            bin_path=self.venv_bin(),
        )

    def _install_uv_pip(self, install):
        args = ["uv", "pip", "install"]

        if install.requirements:
            args.extend(["-r", install.requirements])
        elif install.path:
            local_path = install.path
            if install.extras and isinstance(install.extras, list):
                local_path = f"{local_path}[{','.join(install.extras)}]"
            args.append(local_path)

        self.build_env.run(
            *args,
            cwd=self.checkout_path,
            bin_path=self.venv_bin(),
        )


class Conda(PythonEnvironment):
    """A Conda / Mamba environment driven by the user's ``environment.yml``."""

    def venv_bin(self, filename=None):
        prefixes = ["$CONDA_ENVS_PATH", "$CONDA_DEFAULT_ENV", "bin"]
        return super().venv_bin(prefixes, filename=filename)

    def conda_bin_name(self):
        """Pick ``mamba`` or ``conda`` based on ``build.tools.python``."""
        return self.config.python_interpreter

    def setup_base(self):
        """Append RTD core deps to ``environment.yml`` and create the env."""
        self._append_core_requirements()
        self._show_environment_yaml()

        self.build_env.run(
            self.conda_bin_name(),
            "env",
            "create",
            "--quiet",
            "--name",
            self.version.slug,
            "--file",
            self.config.conda.environment,
            bin_path=None,
            cwd=self.checkout_path,
        )

    def _show_environment_yaml(self):
        """``cat`` the user's ``environment.yml`` into the build log."""
        self.build_env.run(
            "cat",
            self.config.conda.environment,
            cwd=self.checkout_path,
        )

    def _append_core_requirements(self):
        """
        Inject sphinx/mkdocs into the user's ``environment.yml`` before create.

        Pinning core deps inside the user's file (vs. a separate
        ``conda install`` pass) lets the user's pins win over ours.
        """
        env_path = os.path.join(self.checkout_path, self.config.conda.environment)
        try:
            # Symlinks are allowed, but only ones resolving inside the
            # checkout: users legitimately symlink this file within their repo,
            # and the runner reads it from the host, so one pointing outside
            # would resolve against the instance's own filesystem.
            with safe_open(
                env_path, "r", allow_symlinks=True, base_path=self.checkout_path
            ) as inputfile:
                if not inputfile:
                    raise UserFileNotFound(
                        message_id=UserFileNotFound.FILE_NOT_FOUND,
                        format_values={"filename": self.config.conda.environment},
                    )
                environment = parse_yaml(inputfile)
        except IOError:
            log.warning("There was an error while reading Conda environment file.")
            return
        except ParseError:
            log.warning("There was an error while parsing Conda environment file.")
            return

        # Append conda deps to ``dependencies`` and pip deps to ``dependencies.pip``.
        pip_requirements, conda_requirements = self._get_core_requirements()
        dependencies = environment.get("dependencies", [])
        pip_dependencies = {"pip": pip_requirements}

        for item in dependencies:
            if isinstance(item, dict) and "pip" in item:
                # ``pip`` may be ``None`` in the user's file.
                pip_requirements.extend(item.get("pip") or [])
                dependencies.remove(item)
                break

        dependencies.append(pip_dependencies)
        dependencies.extend(conda_requirements)
        environment.update({"dependencies": dependencies})

        try:
            # Same containment as the read above — this is a *write* into a
            # path the user controls.
            with safe_open(
                env_path, "w", allow_symlinks=True, base_path=self.checkout_path
            ) as outputfile:
                yaml.safe_dump(environment, outputfile)
        except IOError:
            log.warning("There was an error while writing the new Conda environment file.")

    def _get_core_requirements(self):
        conda_requirements = []
        pip_requirements = []

        if self.config.doctype == "mkdocs":
            pip_requirements.append("mkdocs")
        else:
            conda_requirements.extend(["sphinx"])

        return pip_requirements, conda_requirements

    def install_core_requirements(self):
        """No-op for conda: core deps were appended to ``environment.yml``."""

    def install_requirements_file(self, install):
        """No-op for conda: deps come exclusively from ``environment.yml``."""

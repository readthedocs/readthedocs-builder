"""
Build configuration parser for ``.readthedocs.yaml``.

Ported from ``readthedocs.config.config``. Changes from upstream:

- ``django.conf.settings`` reads → :mod:`builder.settings`.
- The implicit-config deprecation brownout gate is dropped: an explicit
  doctype configuration is now required unconditionally (``require_config``).
- ``readthedocs.projects.constants.GENERIC`` → :mod:`builder.constants`.
"""

import copy
import os
import re
from contextlib import contextmanager
from functools import lru_cache

from pydantic import BaseModel

from builder import settings
from builder.constants import GENERIC
from builder.filesystem import safe_open

from .exceptions import ConfigError
from .exceptions import ConfigValidationError
from .find import find_one
from .models import BuildJobs
from .models import BuildJobsBuildTypes
from .models import BuildTool
from .models import BuildWithOs
from .models import Conda
from .models import Mkdocs
from .models import Python
from .models import PythonInstall
from .models import Search
from .models import Sphinx
from .models import Submodules
from .models import UvInstall
from .parser import ParseError
from .parser import parse
from .utils import list_to_dict
from .validation import validate_bool
from .validation import validate_choice
from .validation import validate_dict
from .validation import validate_list
from .validation import validate_path
from .validation import validate_path_pattern
from .validation import validate_string


__all__ = (
    "ALL",
    "load",
    "BuildConfigV2",
    "PIP",
    "SETUPTOOLS",
    "UV",
    "LATEST_CONFIGURATION_VERSION",
    "CONFIG_FILENAME_REGEX",
)


ALL = "all"
PIP = "pip"
SETUPTOOLS = "setuptools"
UV = "uv"
CONFIG_FILENAME_REGEX = r"^\.?readthedocs.ya?ml$"

LATEST_CONFIGURATION_VERSION = 2


class BuildConfigBase:
    """
    Config that handles the build of one particular documentation.

    Call :meth:`validate` before using any of the public attributes.

    :param raw_config: dict with the unvalidated configuration.
    :param source_file: file the configuration was loaded from. All paths are
        relative to this file. If a directory is given, the configuration was
        loaded from another source (e.g. the web admin) and ``base_path``
        defaults to that directory.
    :param base_path: explicit base path; overrides the path derived from
        ``source_file`` when set.
    :param require_config: enforce that the config declares an explicit doctype
        configuration (``sphinx.configuration`` / ``mkdocs.configuration``) or
        an alternative (``build.commands`` / overriding build jobs). Defaults
        to ``True`` — the deprecation of implicit config completed long ago
        (https://about.readthedocs.com/blog/2024/12/deprecate-config-files-without-sphinx-or-mkdocs-config/).
        Overridable so tests can exercise the pre-enforcement config shape.
    """

    PUBLIC_ATTRIBUTES = [
        "version",
        "formats",
        "python",
        "conda",
        "build",
        "doctype",
        "sphinx",
        "mkdocs",
        "submodules",
        "search",
    ]

    version = None

    def __init__(
        self,
        raw_config,
        source_file,
        base_path=None,
        require_config=None,
    ):
        self._raw_config = copy.deepcopy(raw_config)
        self.source_config = copy.deepcopy(raw_config)
        self.source_file = source_file
        if base_path:
            self.base_path = base_path
        else:
            if os.path.isdir(self.source_file):
                self.base_path = self.source_file
            else:
                self.base_path = os.path.dirname(self.source_file)

        self._config = {}

        # An explicit doctype configuration is mandatory (see ``require_config``
        # in the class docstring). Enforced by default; only tests opt out.
        self.require_config = True if require_config is None else require_config

    @contextmanager
    def catch_validation_error(self, key):
        """
        Catch a :class:`ConfigValidationError` and re-raise as :class:`ConfigError`.

        Decorates the underlying error with the config-file ``key`` being
        validated and the relative source-file path so the user can locate it.
        """
        try:
            yield
        except ConfigValidationError as error:
            format_values = getattr(error, "format_values", {}) or {}
            format_values.update(
                {
                    "key": key,
                    "value": format_values.get("value"),
                    "source_file": os.path.relpath(self.source_file, self.base_path),
                }
            )
            raise ConfigError(
                message_id=error.message_id,
                format_values=format_values,
            ) from error

    def pop(self, name, container, default, raise_ex):
        """
        Recursively pop ``name`` from ``container``.

        ``name`` is a list (e.g. ``['build', 'tools', 'python']``). Empty
        intermediate dicts are also popped on the way back up.
        """
        key = name[0]
        validate_dict(container)
        if key in container:
            if len(name) > 1:
                value = self.pop(name[1:], container[key], default, raise_ex)
                if not container[key]:
                    container.pop(key)
            else:
                value = container.pop(key)
            return value
        if raise_ex:
            raise ConfigValidationError(
                message_id=ConfigValidationError.VALUE_NOT_FOUND,
                format_values={"value": key},
            )
        return default

    def pop_config(self, key, default=None, raise_ex=False):
        """Pop a dotted key (``key.innerkey``) from ``self._raw_config``."""
        return self.pop(key.split("."), self._raw_config, default, raise_ex)

    def validate(self):
        raise NotImplementedError()

    @property
    def is_using_conda(self):
        return self.python_interpreter in ("conda", "mamba")

    @property
    def is_using_build_commands(self):
        return self.build.commands != []

    @property
    def is_using_setup_py_install(self):
        """True if any python.install entry uses ``method: setuptools``."""
        for install in self.python.install:
            if isinstance(install, PythonInstall) and install.method == SETUPTOOLS:
                return True
        return False

    @property
    def is_using_uv(self):
        """True if any python.install entry uses ``method: uv``."""
        for install in self.python.install:
            if isinstance(install, UvInstall):
                return True
        return False

    @property
    def python_interpreter(self):
        tool = self.build.tools.get("python")
        if tool and tool.version.startswith("mamba"):
            return "mamba"
        if tool and (tool.version.startswith("miniconda") or tool.version.startswith("miniforge")):
            return "conda"
        if tool:
            return "python"
        return None

    @property
    def docker_image(self):
        return self.settings["os"][self.build.os]

    def as_dict(self):
        config = {}
        for name in self.PUBLIC_ATTRIBUTES:
            attr = getattr(self, name)
            config[name] = attr.model_dump() if isinstance(attr, BaseModel) else attr
        return config

    def __getattr__(self, name):
        """Raise a clear error for unknown config attributes."""
        raise ConfigError(
            message_id=ConfigError.KEY_NOT_SUPPORTED_IN_VERSION,
            format_values={"key": name},
        )


class BuildConfigV2(BuildConfigBase):
    """Version 2 of the configuration file."""

    version = "2"
    valid_formats = ["htmlzip", "pdf", "epub"]
    valid_sphinx_builders = {
        "html": "sphinx",
        "htmldir": "sphinx_htmldir",
        "dirhtml": "sphinx_htmldir",
        "singlehtml": "sphinx_singlehtml",
    }

    @property
    def settings(self):
        return settings.RTD_DOCKER_BUILD_SETTINGS

    def validate(self):
        """Validate and process ``raw_config`` in place."""
        self._config["formats"] = self.validate_formats()
        # validate_build must run before validate_python and validate_conda.
        self._config["build"] = self.validate_build()
        self._config["conda"] = self.validate_conda()
        self._config["python"] = self.validate_python()
        # validate_doc_types runs before validate_sphinx / validate_mkdocs.
        self.validate_doc_types()
        self._config["mkdocs"] = self.validate_mkdocs()
        self._config["sphinx"] = self.validate_sphinx()
        self._config["submodules"] = self.validate_submodules()
        self._config["search"] = self.validate_search()
        if self.require_config:
            self.validate_required_config()
        self.validate_keys()

    def validate_formats(self):
        """``formats: all`` expands to all valid formats; otherwise validate the list."""
        formats = self.pop_config("formats", [])
        if formats == ALL:
            return self.valid_formats
        with self.catch_validation_error("formats"):
            validate_list(formats)
            for format_ in formats:
                validate_choice(format_, self.valid_formats)
        return formats

    def validate_conda(self):
        raw_conda = self._raw_config.get("conda")
        if raw_conda is None:
            if self.is_using_conda and not self.is_using_build_commands:
                raise ConfigError(
                    message_id=ConfigError.CONDA_KEY_REQUIRED,
                    format_values={"key": "conda"},
                )
            return None

        with self.catch_validation_error("conda"):
            validate_dict(raw_conda)

        conda = {}
        with self.catch_validation_error("conda.environment"):
            environment = self.pop_config("conda.environment", raise_ex=True)
            conda["environment"] = validate_path(environment, self.base_path)
        return conda

    def validate_build_config_with_os(self):
        """
        Validate the ``build`` block.

        At least one of ``build.tools`` or ``build.commands`` must be set.
        """
        build = {}
        with self.catch_validation_error("build.os"):
            build_os = self.pop_config("build.os", raise_ex=True)
            build["os"] = validate_choice(build_os, self.settings["os"].keys())

        tools = {}
        with self.catch_validation_error("build.tools"):
            tools = self.pop_config("build.tools")
            if tools:
                validate_dict(tools)
                for tool in tools.keys():
                    validate_choice(tool, self.settings["tools"].keys())

        jobs = {}
        with self.catch_validation_error("build.jobs"):
            jobs = self.pop_config("build.jobs", default={})
            validate_dict(jobs)
            valid_jobs = list(BuildJobs.model_fields.keys())
            for job in jobs.keys():
                validate_choice(job, valid_jobs)

        commands = []
        with self.catch_validation_error("build.commands"):
            commands = self.pop_config("build.commands", default=[])
            validate_list(commands)

        if not (tools or commands):
            raise ConfigError(
                message_id=ConfigError.NOT_BUILD_TOOLS_OR_COMMANDS,
                format_values={"key": "build"},
            )

        if commands and jobs:
            raise ConfigError(
                message_id=ConfigError.BUILD_JOBS_AND_COMMANDS,
                format_values={"key": "build"},
            )

        build["jobs"] = {}

        with self.catch_validation_error("build.jobs.build"):
            build["jobs"]["build"] = self.validate_build_jobs_build(jobs)
        # The "build" key was already validated above; remove it before
        # treating remaining jobs as ``list[str]``.
        jobs.pop("build", None)

        for job, job_commands in jobs.items():
            with self.catch_validation_error(f"build.jobs.{job}"):
                build["jobs"][job] = [
                    validate_string(job_command) for job_command in validate_list(job_commands)
                ]

        build["commands"] = []
        for command in commands:
            with self.catch_validation_error("build.commands"):
                build["commands"].append(validate_string(command))

        build["tools"] = {}
        if tools:
            for tool, version in tools.items():
                with self.catch_validation_error(f"build.tools.{tool}"):
                    build["tools"][tool] = validate_choice(
                        version,
                        self.settings["tools"][tool].keys(),
                    )

        build["apt_packages"] = self.validate_apt_packages()
        return build

    def validate_build_jobs_build(self, build_jobs):
        result = {}
        build_jobs_build = build_jobs.get("build", {})
        validate_dict(build_jobs_build)

        allowed_build_types = list(BuildJobsBuildTypes.model_fields.keys())
        for build_type, build_commands in build_jobs_build.items():
            validate_choice(build_type, allowed_build_types)
            if build_type != "html" and build_type not in self.formats:
                raise ConfigError(
                    message_id=ConfigError.BUILD_JOBS_BUILD_TYPE_MISSING_IN_FORMATS,
                    format_values={"build_type": build_type},
                )
            with self.catch_validation_error(f"build.jobs.build.{build_type}"):
                result[build_type] = [
                    validate_string(build_command)
                    for build_command in validate_list(build_commands)
                ]
        return result

    def validate_apt_packages(self):
        apt_packages = []
        with self.catch_validation_error("build.apt_packages"):
            raw_packages = self._raw_config.get("build", {}).get("apt_packages", [])
            validate_list(raw_packages)
            self._raw_config.setdefault("build", {})["apt_packages"] = list_to_dict(raw_packages)
            apt_packages = [self.validate_apt_package(index) for index in range(len(raw_packages))]
            if not raw_packages:
                self.pop_config("build.apt_packages")
        return apt_packages

    def validate_build(self):
        raw_build = self._raw_config.get("build", {})
        with self.catch_validation_error("build"):
            validate_dict(raw_build)
        return self.validate_build_config_with_os()

    def validate_apt_package(self, index):
        """
        Reject apt packages that look like options or paths.

        Only allows the canonical Debian package name pattern, prevents
        injection of extra ``apt-get install`` options or paths to ``.deb``.
        """
        key = f"build.apt_packages.{index}"
        package = self.pop_config(key)
        with self.catch_validation_error(key):
            validate_string(package)
            package = package.strip()
            invalid_starts = ["-", "/", "."]
            for start in invalid_starts:
                if package.startswith(start):
                    raise ConfigError(
                        message_id=ConfigError.APT_INVALID_PACKAGE_NAME_PREFIX,
                        format_values={
                            "prefix": start,
                            "package": package,
                            "key": key,
                        },
                    )
            pattern = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.+-]*$")
            if not pattern.match(package):
                raise ConfigError(
                    message_id=ConfigError.APT_INVALID_PACKAGE_NAME,
                    format_values={"package": package, "key": key},
                )
        return package

    def validate_python(self):
        """
        Validate ``python.install``.

        ``validate_build`` must have run first.
        """
        raw_python = self._raw_config.get("python", {})
        with self.catch_validation_error("python"):
            validate_dict(raw_python)

        python = {}
        with self.catch_validation_error("python.install"):
            raw_install = self._raw_config.get("python", {}).get("install", [])
            validate_list(raw_install)
            if raw_install:
                self._raw_config.setdefault("python", {})["install"] = list_to_dict(raw_install)
            else:
                self.pop_config("python.install")

        raw_install = self._raw_config.get("python", {}).get("install", [])
        python["install"] = [
            self.validate_python_install(index) for index in range(len(raw_install))
        ]

        # uv entries currently support a single python.install item only.
        has_uv_install = any(
            install.get("method") == UV
            for install in python["install"]
            if isinstance(install, dict)
        )
        if has_uv_install and len(python["install"]) > 1:
            raise ConfigError(
                message_id=ConfigError.UV_MULTIPLE_INSTALL_ENTRIES_INVALID,
            )

        return python

    def validate_python_install(self, index):
        """Validate a single ``python.install[<index>]`` entry."""
        python_install = {}
        key = f"python.install.{index}"
        raw_install = self._raw_config["python"]["install"][str(index)]
        with self.catch_validation_error(key):
            validate_dict(raw_install)

        method = raw_install.get("method", PIP)
        has_requirements = "requirements" in raw_install
        has_path = "path" in raw_install

        # Case 1: method: uv
        if method == UV:
            return self._validate_uv_install(key)

        # Case 2: requirements file (legacy pip)
        if has_requirements:
            requirements_key = key + ".requirements"
            with self.catch_validation_error(requirements_key):
                requirements = validate_path(
                    self.pop_config(requirements_key),
                    self.base_path,
                )
                python_install["requirements"] = requirements
            return python_install

        # Case 3: path-based install (pip/setuptools)
        if has_path:
            path_key = key + ".path"
            with self.catch_validation_error(path_key):
                path = validate_path(self.pop_config(path_key), self.base_path)
                python_install["path"] = path

            method_key = key + ".method"
            with self.catch_validation_error(method_key):
                method = validate_choice(
                    self.pop_config(method_key, PIP),
                    [PIP, SETUPTOOLS, UV],
                )
                python_install["method"] = method

            extra_req_key = key + ".extra_requirements"
            with self.catch_validation_error(extra_req_key):
                extra_requirements = validate_list(
                    self.pop_config(extra_req_key, []),
                )
                extra_requirements = [validate_string(element) for element in extra_requirements]
                if extra_requirements and python_install["method"] != PIP:
                    raise ConfigError(
                        message_id=ConfigError.USE_PIP_FOR_EXTRA_REQUIREMENTS,
                    )
                python_install["extra_requirements"] = extra_requirements
            return python_install

        raise ConfigError(
            message_id=ConfigError.PIP_PATH_OR_REQUIREMENT_REQUIRED,
            format_values={"key": key},
        )

    def _validate_uv_install(self, key):
        """Validate a ``method: uv`` python.install entry."""
        method_key = key + ".method"
        self.pop_config(method_key)
        python_install = {"method": UV}

        command_key = key + ".command"
        with self.catch_validation_error(command_key):
            command = self.pop_config(command_key)
            if command is None:
                raise ConfigError(message_id=ConfigError.UV_COMMAND_REQUIRED)
            command = validate_choice(command, ["sync", "pip"])
            python_install["command"] = command

        path_key = key + ".path"
        path = self.pop_config(path_key)
        with self.catch_validation_error(path_key):
            if path:
                path = validate_path(path, self.base_path)
            python_install["path"] = path

        requirements_key = key + ".requirements"
        requirements = self.pop_config(requirements_key)

        groups_key = key + ".groups"
        groups = self.pop_config(groups_key, None)

        extras_key = key + ".extras"
        extras = self.pop_config(extras_key)

        if command == "sync":
            if requirements is not None:
                raise ConfigError(message_id=ConfigError.UV_SYNC_REQUIREMENTS_INVALID)

            if groups is not None:
                with self.catch_validation_error(groups_key):
                    if isinstance(groups, list):
                        if not groups:
                            raise ConfigError(
                                message_id=ConfigError.UV_GROUPS_EXTRAS_EMPTY,
                                format_values={"field": "groups"},
                            )
                        groups = validate_list(groups)
                    elif groups == ALL:
                        pass
                    else:
                        raise ConfigError(
                            message_id=ConfigError.UV_GROUPS_EXTRAS_INVALID_TYPE,
                            format_values={"field": "groups"},
                        )
                    python_install["groups"] = groups

            if extras is not None:
                with self.catch_validation_error(extras_key):
                    if isinstance(extras, list):
                        if not extras:
                            raise ConfigError(
                                message_id=ConfigError.UV_GROUPS_EXTRAS_EMPTY,
                                format_values={"field": "extras"},
                            )
                        extras = validate_list(extras)
                    elif extras == ALL:
                        pass
                    else:
                        raise ConfigError(
                            message_id=ConfigError.UV_GROUPS_EXTRAS_INVALID_TYPE,
                            format_values={"field": "extras"},
                        )
                    python_install["extras"] = extras

        else:  # command == "pip"
            if groups is not None:
                raise ConfigError(message_id=ConfigError.UV_PIP_GROUPS_NOT_ALLOWED)

            if requirements is None and path is None:
                raise ConfigError(
                    message_id=ConfigError.UV_PIP_REQUIREMENTS_OR_PATH_REQUIRED,
                )

            if requirements is not None and path is not None:
                raise ConfigError(
                    message_id=ConfigError.UV_PIP_REQUIREMENTS_AND_PATH_MUTUALLY_EXCLUSIVE,
                )

            if requirements is not None:
                with self.catch_validation_error(requirements_key):
                    requirements = validate_path(requirements, self.base_path)
                    python_install["requirements"] = requirements

            if extras is not None:
                with self.catch_validation_error(extras_key):
                    if isinstance(extras, list):
                        if not extras:
                            raise ConfigError(
                                message_id=ConfigError.UV_GROUPS_EXTRAS_EMPTY,
                                format_values={"field": "extras"},
                            )
                        extras = validate_list(extras)
                    elif extras == ALL:
                        pass
                    else:
                        raise ConfigError(
                            message_id=ConfigError.UV_GROUPS_EXTRAS_INVALID_TYPE,
                            format_values={"field": "extras"},
                        )
                    python_install["extras"] = extras

        return python_install

    def validate_doc_types(self):
        """``sphinx`` and ``mkdocs`` are mutually exclusive."""
        with self.catch_validation_error("."):
            if "sphinx" in self._raw_config and "mkdocs" in self._raw_config:
                raise ConfigError(
                    message_id=ConfigError.SPHINX_MKDOCS_CONFIG_TOGETHER,
                )

    def validate_mkdocs(self):
        raw_mkdocs = self._raw_config.get("mkdocs")
        if raw_mkdocs is None:
            return None

        with self.catch_validation_error("mkdocs"):
            validate_dict(raw_mkdocs)

        mkdocs = {}
        with self.catch_validation_error("mkdocs.configuration"):
            configuration = self.pop_config("mkdocs.configuration", None)
            if configuration is not None:
                configuration = validate_path(configuration, self.base_path)
            mkdocs["configuration"] = configuration

        with self.catch_validation_error("mkdocs.fail_on_warning"):
            fail_on_warning = self.pop_config("mkdocs.fail_on_warning", False)
            mkdocs["fail_on_warning"] = validate_bool(fail_on_warning)

        return mkdocs

    def validate_sphinx(self):
        raw_sphinx = self._raw_config.get("sphinx")
        if raw_sphinx is None:
            if self.mkdocs is None:
                raw_sphinx = {}
            else:
                return None

        with self.catch_validation_error("sphinx"):
            validate_dict(raw_sphinx)

        sphinx = {}
        with self.catch_validation_error("sphinx.builder"):
            builder = validate_choice(
                self.pop_config("sphinx.builder", "html"),
                self.valid_sphinx_builders.keys(),
            )
            sphinx["builder"] = self.valid_sphinx_builders[builder]

        with self.catch_validation_error("sphinx.configuration"):
            configuration = self.pop_config("sphinx.configuration")
            if configuration is not None:
                configuration = validate_path(configuration, self.base_path)
                if os.path.basename(configuration) != "conf.py":
                    raise ConfigError(
                        message_id=ConfigError.SPHINX_INVALID_CONFIG_FILE,
                    )
            sphinx["configuration"] = configuration

        with self.catch_validation_error("sphinx.fail_on_warning"):
            fail_on_warning = self.pop_config("sphinx.fail_on_warning", False)
            sphinx["fail_on_warning"] = validate_bool(fail_on_warning)

        return sphinx

    def validate_submodules(self):
        """
        Validate ``submodules.include`` / ``submodules.exclude``.

        Both can be ``ALL``; they cannot be set simultaneously.
        """
        raw_submodules = self._raw_config.get("submodules", {})
        with self.catch_validation_error("submodules"):
            validate_dict(raw_submodules)

        submodules = {}
        with self.catch_validation_error("submodules.include"):
            include = self.pop_config("submodules.include", [])
            if include != ALL:
                include = [validate_string(submodule) for submodule in validate_list(include)]
            submodules["include"] = include

        with self.catch_validation_error("submodules.exclude"):
            default = [] if submodules["include"] else ALL
            exclude = self.pop_config("submodules.exclude", default)
            if exclude != ALL:
                exclude = [validate_string(submodule) for submodule in validate_list(exclude)]
            submodules["exclude"] = exclude

        with self.catch_validation_error("submodules"):
            is_including = bool(submodules["include"])
            is_excluding = submodules["exclude"] == ALL or bool(submodules["exclude"])
            if is_including and is_excluding:
                raise ConfigError(
                    message_id=ConfigError.SUBMODULES_INCLUDE_EXCLUDE_TOGETHER,
                )

        with self.catch_validation_error("submodules.recursive"):
            recursive = self.pop_config("submodules.recursive", False)
            submodules["recursive"] = validate_bool(recursive)

        return submodules

    def validate_search(self):
        """
        Validate the ``search`` block.

        - ``ranking`` is a map of path patterns → integer rank in [-10, 10].
        - ``ignore`` is a list of path patterns (basic globs).
        """
        raw_search = self._raw_config.get("search", {})
        with self.catch_validation_error("search"):
            validate_dict(raw_search)

        search = {}
        with self.catch_validation_error("search.ranking"):
            ranking = self.pop_config("search.ranking", {})
            validate_dict(ranking)

            valid_rank_range = list(range(-10, 10 + 1))

            final_ranking = {}
            for pattern, rank in ranking.items():
                pattern = validate_path_pattern(pattern)
                validate_choice(rank, valid_rank_range)
                final_ranking[pattern] = rank

            search["ranking"] = final_ranking

        with self.catch_validation_error("search.ignore"):
            ignore_default = [
                "search.html",
                "search/index.html",
                "404.html",
                "404/index.html",
            ]
            search_ignore = self.pop_config("search.ignore", ignore_default)
            validate_list(search_ignore)
            final_ignore = [validate_path_pattern(pattern) for pattern in search_ignore]
            search["ignore"] = final_ignore

        return search

    def validate_required_config(self):
        """
        Require an explicit doctype configuration.

        A config must declare ``sphinx.configuration`` or
        ``mkdocs.configuration``, or opt out of the default doctype build via
        ``build.commands`` / overriding build jobs. Enforced unless
        ``require_config`` was disabled (tests only).
        """
        if self.is_using_build_commands:
            return

        has_sphinx_key = "sphinx" in self.source_config
        has_mkdocs_key = "mkdocs" in self.source_config
        if has_sphinx_key and not self.sphinx.configuration:
            raise ConfigError(message_id=ConfigError.SPHINX_CONFIG_MISSING)

        if has_mkdocs_key and not self.mkdocs.configuration:
            raise ConfigError(message_id=ConfigError.MKDOCS_CONFIG_MISSING)

        if not self.new_jobs_overriden and not has_sphinx_key and not has_mkdocs_key:
            raise ConfigError(message_id=ConfigError.SPHINX_CONFIG_MISSING)

    @property
    def new_jobs_overriden(self):
        """True if the user overrides any of the new build jobs."""
        build_jobs = self.build.jobs
        new_jobs = (
            build_jobs.create_environment,
            build_jobs.install,
            build_jobs.build.html,
            build_jobs.build.pdf,
            build_jobs.build.epub,
            build_jobs.build.htmlzip,
        )
        for job in new_jobs:
            if job is not None:
                return True
        return False

    def validate_keys(self):
        """
        Reject any config key that wasn't consumed by a ``validate_*`` method.

        Run after all other validations have popped their keys.
        """
        # ``version`` is validated in :func:`load`, not popped earlier.
        self.pop_config("version", None)
        wrong_key = ".".join(self._get_extra_key(self._raw_config))
        if wrong_key:
            raise ConfigError(
                message_id=ConfigError.INVALID_KEY_NAME,
                format_values={"key": wrong_key},
            )

    def _get_extra_key(self, value):
        """Return the dotted path of the first leftover key in ``value``."""
        if isinstance(value, dict) and value:
            key_name = next(iter(value))
            return [key_name] + self._get_extra_key(value[key_name])
        return []

    @property
    def formats(self):
        return self._config["formats"]

    @property
    def conda(self):
        if self._config["conda"]:
            return Conda(**self._config["conda"])
        return None

    @property
    @lru_cache(maxsize=1)
    def build(self):
        build = self._config["build"]
        tools = {
            tool: BuildTool(
                version=version,
                full_version=self.settings["tools"][tool][version],
            )
            for tool, version in build["tools"].items()
        }
        return BuildWithOs(
            os=build["os"],
            tools=tools,
            jobs=BuildJobs(**build["jobs"]),
            commands=build["commands"],
            apt_packages=build["apt_packages"],
        )

    @property
    def python(self):
        return Python(**self._config["python"])

    @property
    def sphinx(self):
        if self._config["sphinx"]:
            return Sphinx(**self._config["sphinx"])
        return None

    @property
    def mkdocs(self):
        if self._config["mkdocs"]:
            return Mkdocs(**self._config["mkdocs"])
        return None

    @property
    def doctype(self):
        if "commands" in self._config["build"] and self._config["build"]["commands"]:
            return GENERIC

        has_sphinx_key = "sphinx" in self.source_config
        has_mkdocs_key = "mkdocs" in self.source_config
        if self.new_jobs_overriden and not has_sphinx_key and not has_mkdocs_key:
            return GENERIC

        if self.mkdocs:
            return "mkdocs"
        return self.sphinx.builder

    @property
    def submodules(self):
        return Submodules(**self._config["submodules"])

    @property
    def search(self):
        return Search(**self._config["search"])


def load(path, readthedocs_yaml_path=None):
    """
    Load and validate the config file under ``path``.

    :param path: directory to search for the config file (typically the repo root).
    :param readthedocs_yaml_path: optional explicit path (relative to ``path``).
    :returns: a validated :class:`BuildConfigV2`.
    """
    if readthedocs_yaml_path:
        filename = os.path.join(path, readthedocs_yaml_path)
        if not os.path.exists(filename):
            raise ConfigError(
                message_id=ConfigError.CONFIG_PATH_NOT_FOUND,
                format_values={"directory": os.path.relpath(filename, path)},
            )
    else:
        filename = find_one(path, CONFIG_FILENAME_REGEX)
        if not filename:
            raise ConfigError(ConfigError.DEFAULT_PATH_NOT_FOUND)

    # ``safe_open`` enforces that a config file which is a symlink resolves
    # inside ``path`` (the repo checkout), preventing a repo from reading files
    # outside it via a symlinked ``.readthedocs.yaml`` (GHSA-368m-86q9-m99w).
    with safe_open(filename, "r", allow_symlinks=True, base_path=path) as configuration_file:
        try:
            config = parse(configuration_file.read())
        except ParseError as error:
            raise ConfigError(
                message_id=ConfigError.SYNTAX_INVALID,
                format_values={
                    "filename": os.path.relpath(filename, path),
                    "error_message": str(error),
                },
            ) from error

        version = config.get("version", 2)
        if version not in (2, "2"):
            raise ConfigError(message_id=ConfigError.INVALID_VERSION)

        build_config = BuildConfigV2(config, source_file=filename)

    build_config.validate()
    return build_config

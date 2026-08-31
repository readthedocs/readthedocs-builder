"""
Pydantic models for the parsed configuration object.

Ported verbatim from ``readthedocs.config.models``. The models are used as
typed containers for the parsed result; runtime validation still happens in
``BuildConfigV2`` (the parser does its own checks via the helpers in
``validation.py``).
"""

from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict


class ConfigBaseModel(BaseModel):
    """Base for all config models. Forbids unknown keys."""

    model_config = ConfigDict(extra="forbid")


class BuildTool(ConfigBaseModel):
    version: str
    full_version: str


class BuildJobsBuildTypes(ConfigBaseModel):
    """Object used for the ``build.jobs.build`` config key."""

    html: list[str] | None = None
    pdf: list[str] | None = None
    epub: list[str] | None = None
    htmlzip: list[str] | None = None


class BuildJobs(ConfigBaseModel):
    """Object used for the ``build.jobs`` config key."""

    pre_checkout: list[str] = []
    post_checkout: list[str] = []
    pre_system_dependencies: list[str] = []
    post_system_dependencies: list[str] = []
    pre_create_environment: list[str] = []
    create_environment: list[str] | None = None
    post_create_environment: list[str] = []
    pre_install: list[str] = []
    install: list[str] | None = None
    post_install: list[str] = []
    pre_build: list[str] = []
    build: BuildJobsBuildTypes = BuildJobsBuildTypes()
    post_build: list[str] = []


class BuildWithOs(ConfigBaseModel):
    os: str
    tools: dict[str, BuildTool]
    jobs: BuildJobs = BuildJobs()
    apt_packages: list[str] = []
    commands: list[str] = []


class PythonInstallRequirements(ConfigBaseModel):
    requirements: str


class PythonInstall(ConfigBaseModel):
    path: str
    method: Literal["pip", "setuptools"] = "pip"
    extra_requirements: list[str] = []


class UvInstall(ConfigBaseModel):
    method: Literal["uv"]
    command: Literal["sync", "pip"]
    path: str | None = None
    requirements: str | None = None
    groups: list[str] | Literal["all"] | None = None
    extras: list[str] | Literal["all"] | None = None


class Python(ConfigBaseModel):
    install: list[PythonInstall | PythonInstallRequirements | UvInstall] = []


class Conda(ConfigBaseModel):
    environment: str


class Sphinx(ConfigBaseModel):
    configuration: str | None
    builder: Literal["sphinx", "sphinx_htmldir", "sphinx_singlehtml"] = "sphinx"
    fail_on_warning: bool = False


class Mkdocs(ConfigBaseModel):
    configuration: str | None
    fail_on_warning: bool = False


class Submodules(ConfigBaseModel):
    include: list[str] | Literal["all"] = []
    exclude: list[str] | Literal["all"] = []
    recursive: bool = False


class Search(ConfigBaseModel):
    ranking: dict[str, int] = {}
    ignore: list[str] = [
        "search.html",
        "search/index.html",
        "404.html",
        "404/index.html",
    ]

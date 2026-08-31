"""
Tests for the Sphinx and MkDocs doc builders.

Ported from ``readthedocs/rtd_tests/tests/test_doc_builder.py``. Differences
from upstream, all intentional:

- Real ``APIProject`` / ``APIVersion`` + a non-recording ``BuildEnvironment``
  replace the Django models and the ``MagicMock`` build env.
- Upstream's conf.py auto-discovery (``Version.get_conf_py_path``) is dropped:
  ``sphinx.configuration`` is mandatory, so the builder only resolves the
  configured path and reports a missing one as ``ProjectConfigurationError``.
"""

import os
from unittest import mock

import pytest
from conftest import make_builder

from builder.backends.mkdocs import MkdocsHTML
from builder.backends.sphinx import BaseSphinx
from builder.backends.sphinx import EpubBuilder
from builder.backends.sphinx import HtmlBuilder
from builder.backends.sphinx import HtmlDirBuilder
from builder.backends.sphinx import LATEX_COMMAND_CLASSES
from builder.backends.sphinx import DockerLatexBuildCommand
from builder.backends.sphinx import LatexBuildCommand
from builder.environments import BuildCommand
from builder.environments import DockerBuildCommand
from builder.environments import DockerBuildEnvironment
from builder.backends.sphinx import LocalMediaBuilder
from builder.backends.sphinx import SingleHtmlBuilder
from builder.backends.sphinx import PdfBuilder
from builder.exceptions import BuildUserError
from builder.exceptions import ProjectConfigurationError
from builder.exceptions import UserFileNotFound
from builder.python_envs import UvEnv
from builder.python_envs import Virtualenv


def _fake_run(builder):
    """Patch a builder's ``run`` with a mock returning a successful command."""
    fake = mock.MagicMock(return_value=mock.MagicMock(successful=True, output=""))
    return mock.patch.object(builder, "run", fake), fake


# ---------------------------------------------------------------------------
# get_sphinx_cmd
# ---------------------------------------------------------------------------


def test_get_sphinx_cmd_uses_uv_run_for_uv_env(docroot):
    builder = make_builder(
        HtmlBuilder,
        config={
            "sphinx": {"configuration": "conf.py"},
            "python": {"install": [{"method": "uv", "command": "sync"}]},
        },
        env_cls=UvEnv,
    )
    assert builder.get_sphinx_cmd() == ("uv", "run", "--no-sync", "--no-dev", "sphinx-build")


def test_get_sphinx_cmd_uses_python_module_for_virtualenv(docroot):
    builder = make_builder(
        HtmlBuilder,
        config={"sphinx": {"configuration": "conf.py"}},
        env_cls=Virtualenv,
    )
    assert builder.get_sphinx_cmd() == (
        builder.python_env.venv_bin(filename="python"),
        "-m",
        "sphinx",
    )


# ---------------------------------------------------------------------------
# Sphinx builder subclasses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder_cls, expected",
    [
        (HtmlBuilder, "html"),
        (HtmlDirBuilder, "dirhtml"),
        (SingleHtmlBuilder, "singlehtml"),
        (LocalMediaBuilder, "singlehtml"),
        (EpubBuilder, "epub"),
    ],
)
def test_sphinx_builder_names(docroot, builder_cls, expected):
    builder = make_builder(builder_cls, config={"sphinx": {"configuration": "conf.py"}})
    assert builder.sphinx_builder == expected


@pytest.mark.parametrize(
    "builder_cls, output_dir",
    [
        (HtmlBuilder, "html"),
        (LocalMediaBuilder, "htmlzip"),
        (EpubBuilder, "epub"),
    ],
)
def test_sphinx_relative_output_dir(docroot, builder_cls, output_dir):
    builder = make_builder(builder_cls, config={"sphinx": {"configuration": "conf.py"}})
    assert builder.relative_output_dir == output_dir


def test_sphinx_config_file_resolves_relative_to_project_path(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "docs/conf.py"}})
    assert builder.config_file == os.path.join(builder.project_path, "docs/conf.py")


# ---------------------------------------------------------------------------
# Sphinx build() command construction
# ---------------------------------------------------------------------------


def test_sphinx_build_command(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "conf.py"}})
    patcher, fake = _fake_run(builder)
    with patcher:
        assert builder.build() is True
    args = fake.call_args[0]
    assert args[-3:] == ("language=en", ".", os.path.join("$READTHEDOCS_OUTPUT", "html"))
    assert "-b" in args
    assert args[args.index("-b") + 1] == "html"


def test_sphinx_build_fail_on_warning_adds_flags(docroot):
    builder = make_builder(
        HtmlBuilder,
        config={"sphinx": {"configuration": "conf.py", "fail_on_warning": True}},
    )
    patcher, fake = _fake_run(builder)
    with patcher:
        builder.build()
    args = fake.call_args[0]
    assert "-W" in args
    assert "--keep-going" in args


def test_sphinx_build_without_fail_on_warning_omits_flags(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "conf.py"}})
    patcher, fake = _fake_run(builder)
    with patcher:
        builder.build()
    args = fake.call_args[0]
    assert "-W" not in args


def test_sphinx_build_passes_the_language(docroot):
    builder = make_builder(
        HtmlBuilder,
        config={"sphinx": {"configuration": "conf.py"}},
        language="es",
    )
    patcher, fake = _fake_run(builder)
    with patcher:
        builder.build()
    assert "language=es" in fake.call_args[0]


def test_sphinx_build_maps_deprecated_language_code(docroot):
    # ``get_language`` normalizes legacy locale codes (e.g. ``pt-br`` -> ``pt_BR``).
    builder = make_builder(
        HtmlBuilder,
        config={"sphinx": {"configuration": "conf.py"}},
        language="pt-br",
    )
    assert builder.get_language(builder.project) == "pt_BR"


def test_sphinx_dirhtml_uses_dirhtml_builder_in_command(docroot):
    builder = make_builder(HtmlDirBuilder, config={"sphinx": {"configuration": "conf.py"}})
    patcher, fake = _fake_run(builder)
    with patcher:
        builder.build()
    args = fake.call_args[0]
    assert args[args.index("-b") + 1] == "dirhtml"


# ---------------------------------------------------------------------------
# Sphinx show_conf
# ---------------------------------------------------------------------------


def test_sphinx_show_conf_cats_the_config_file(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "conf.py"}})
    os.makedirs(builder.project_path, exist_ok=True)
    open(os.path.join(builder.project_path, "conf.py"), "w").close()

    patcher, fake = _fake_run(builder)
    with patcher:
        builder.show_conf()
    assert fake.call_args[0][0] == "cat"


def test_sphinx_show_conf_raises_when_the_declared_file_is_missing(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "conf.py"}})
    os.makedirs(builder.project_path, exist_ok=True)
    with pytest.raises(UserFileNotFound) as excinfo:
        builder.show_conf()
    assert excinfo.value.message_id == UserFileNotFound.FILE_NOT_FOUND


def test_sphinx_without_configuration_reports_missing_conf(docroot):
    # A config without ``sphinx.configuration`` is rejected at config validation
    # in production (see test_config: required doctype configuration). If one
    # reaches the builder anyway, show_conf() reports it as a clean
    # ProjectConfigurationError rather than crashing.
    builder = make_builder(HtmlBuilder, config={})
    with pytest.raises(ProjectConfigurationError) as excinfo:
        builder.show_conf()
    assert excinfo.value.message_id == ProjectConfigurationError.NOT_FOUND


# ---------------------------------------------------------------------------
# LatexBuildCommand
# ---------------------------------------------------------------------------


def test_latex_build_command_promotes_exit_code_when_pdf_written():
    cmd = LatexBuildCommand(["/bin/bash", "-c", "echo 'Output written on foo.pdf'; exit 1"], cwd="/tmp")
    cmd.run()
    # LaTeX exits non-zero but wrote a PDF, so the exit code is forced to 0.
    assert cmd.exit_code == 0


def test_latex_build_command_keeps_exit_code_without_pdf():
    cmd = LatexBuildCommand(["/bin/bash", "-c", "echo no output here; exit 1"], cwd="/tmp")
    cmd.run()
    assert cmd.exit_code == 1


def test_latex_variant_follows_the_environments_command_class():
    """
    PDF builds must run wherever every other command runs.

    Picking the local variant under a Docker environment would silently run
    LaTeX on the host, outside the build container.
    """
    assert LATEX_COMMAND_CLASSES[BuildCommand] is LatexBuildCommand
    assert LATEX_COMMAND_CLASSES[DockerBuildCommand] is DockerLatexBuildCommand
    assert LATEX_COMMAND_CLASSES[DockerBuildEnvironment.command_class] is DockerLatexBuildCommand


def test_docker_latex_build_command_promotes_exit_code_when_pdf_written():
    build_env = DockerBuildEnvironment(record=False, container_name="build-1")
    client = mock.Mock()
    client.exec_create.return_value = {"Id": "exec-id"}
    client.exec_start.return_value = b"Output written on foo.pdf"
    client.exec_inspect.return_value = {"ExitCode": 1}
    build_env.client = client

    cmd = DockerLatexBuildCommand(("latexmk",), build_env=build_env)
    cmd.run()

    assert cmd.exit_code == 0


# ---------------------------------------------------------------------------
# Post-build packaging (htmlzip / epub / pdf)
# ---------------------------------------------------------------------------


def test_localmedia_post_build_zips_from_a_renamed_tmpdir(docroot):
    # SECURITY CRITICAL (GHSA-hqwg-gjqw-h5wg): the output is moved into a
    # tmpdir subdir named ``<slug>-<version>`` so zip stores that relative
    # arcname instead of an absolute path. Pin that sequence.
    builder = make_builder(LocalMediaBuilder, config={"sphinx": {"configuration": "conf.py"}})
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        # First call is ``mktemp --directory``; hand back a fake temp dir.
        output = "/tmp/fake-tmpdir" if args[0] == "mktemp" else ""
        return mock.MagicMock(successful=True, output=output)

    with mock.patch.object(builder, "run", side_effect=fake_run):
        builder._post_build()

    zip_call = next(c for c in calls if c[0][0] == "zip")
    zip_args, zip_kwargs = zip_call
    assert "pip-latest" in zip_args
    assert zip_kwargs["cwd"] == "/tmp/fake-tmpdir"


def test_epub_post_build_is_a_noop_without_an_epub(docroot):
    builder = make_builder(EpubBuilder, config={"sphinx": {"configuration": "conf.py"}})
    # The output dir has no ``*.epub``, so nothing is moved.
    with mock.patch.object(builder, "run") as run:
        builder._post_build()
    run.assert_not_called()


def test_pdf_build_raises_when_no_tex_file_is_produced(docroot):
    builder = make_builder(PdfBuilder, config={"sphinx": {"configuration": "conf.py"}})
    with mock.patch.object(builder, "run", return_value=mock.MagicMock(successful=True)):
        with pytest.raises(BuildUserError) as excinfo:
            builder.build()
    assert excinfo.value.message_id == BuildUserError.TEX_FILE_NOT_FOUND


def test_pdf_post_build_raises_without_a_pdf_file_name(docroot):
    builder = make_builder(PdfBuilder, config={"sphinx": {"configuration": "conf.py"}})
    # ``_build_latexmk`` never ran, so ``pdf_file_name`` is unset.
    with pytest.raises(BuildUserError) as excinfo:
        builder._post_build()
    assert excinfo.value.message_id == BuildUserError.PDF_NOT_FOUND


# ---------------------------------------------------------------------------
# MkDocs
# ---------------------------------------------------------------------------


def test_get_mkdocs_cmd_uses_uv_run_for_uv_env(docroot):
    builder = make_builder(
        MkdocsHTML,
        config={
            "mkdocs": {"configuration": "mkdocs.yml"},
            "python": {"install": [{"method": "uv", "command": "sync"}]},
        },
        env_cls=UvEnv,
    )
    assert builder.get_mkdocs_cmd() == ("uv", "run", "--no-sync", "--no-dev", "mkdocs")


def test_get_mkdocs_cmd_uses_python_module_for_virtualenv(docroot):
    builder = make_builder(MkdocsHTML, config={"mkdocs": {"configuration": "mkdocs.yml"}})
    cmd = builder.get_mkdocs_cmd()
    assert cmd[-2:] == ("-m", "mkdocs")
    assert cmd[0] == builder.python_env.venv_bin(filename="python")


def test_mkdocs_config_file_resolves_relative_to_project_path(docroot):
    builder = make_builder(MkdocsHTML, config={"mkdocs": {"configuration": "mkdocs.yml"}})
    assert builder.config_file == os.path.join(builder.project_path, "mkdocs.yml")


def test_mkdocs_build_command(docroot):
    builder = make_builder(MkdocsHTML, config={"mkdocs": {"configuration": "mkdocs.yml"}})
    patcher, fake = _fake_run(builder)
    with patcher:
        assert builder.build() is True
    args = fake.call_args[0]
    assert args[0:2] == (builder.python_env.venv_bin(filename="python"), "-m")
    assert "build" in args
    assert "--clean" in args


def test_mkdocs_build_fail_on_warning_adds_strict(docroot):
    builder = make_builder(
        MkdocsHTML,
        config={"mkdocs": {"configuration": "mkdocs.yml", "fail_on_warning": True}},
    )
    patcher, fake = _fake_run(builder)
    with patcher:
        builder.build()
    assert "--strict" in fake.call_args[0]


def test_mkdocs_build_without_fail_on_warning_omits_strict(docroot):
    builder = make_builder(MkdocsHTML, config={"mkdocs": {"configuration": "mkdocs.yml"}})
    patcher, fake = _fake_run(builder)
    with patcher:
        builder.build()
    assert "--strict" not in fake.call_args[0]


def test_mkdocs_show_conf_raises_when_the_config_is_missing(docroot):
    builder = make_builder(MkdocsHTML, config={"mkdocs": {"configuration": "mkdocs.yml"}})
    os.makedirs(builder.project_path, exist_ok=True)
    with pytest.raises(UserFileNotFound) as excinfo:
        builder.show_conf()
    assert excinfo.value.message_id == UserFileNotFound.FILE_NOT_FOUND
    assert excinfo.value.format_values.get("filename") == "mkdocs.yml"


def test_mkdocs_show_conf_cats_the_config_file(docroot):
    builder = make_builder(MkdocsHTML, config={"mkdocs": {"configuration": "mkdocs.yml"}})
    os.makedirs(builder.project_path, exist_ok=True)
    open(os.path.join(builder.project_path, "mkdocs.yml"), "w").close()
    patcher, fake = _fake_run(builder)
    with patcher:
        builder.show_conf()
    assert fake.call_args[0][0] == "cat"


# ---------------------------------------------------------------------------
# BaseBuilder.docs_dir
# ---------------------------------------------------------------------------


def test_docs_dir_finds_a_docs_subdirectory(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "conf.py"}})
    os.makedirs(os.path.join(builder.project_path, "docs"), exist_ok=True)
    assert builder.docs_dir() == os.path.join(builder.project_path, "docs")


def test_docs_dir_falls_back_to_the_project_path(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "conf.py"}})
    os.makedirs(builder.project_path, exist_ok=True)
    assert builder.docs_dir() == builder.project_path


def test_get_final_doctype_returns_the_config_doctype(docroot):
    builder = make_builder(HtmlBuilder, config={"sphinx": {"configuration": "conf.py"}})
    assert builder.get_final_doctype() == "sphinx"

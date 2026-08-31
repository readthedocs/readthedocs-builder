"""
Sphinx backends.

Ported from ``readthedocs.doc_builder.backends.sphinx``. Drops the
host/container path split (the docroot is mounted at the same path on both
sides). Commands reference ``$READTHEDOCS_OUTPUT`` /
``$READTHEDOCS_VIRTUALENV_PATH`` etc., which the container's shell expands —
see ``DockerBuildCommand._escape_command``.
"""

import itertools
import os
from glob import glob
from pathlib import Path

import structlog

from builder.base import BaseBuilder
from builder.constants import OLD_LANGUAGES_CODE_MAPPING
from builder.constants import PDF_RE
from builder.constants import Feature
from builder.environments import BuildCommand
from builder.environments import DockerBuildCommand
from builder.exceptions import BuildUserError
from builder.exceptions import ProjectConfigurationError
from builder.exceptions import UserFileNotFound
from builder.python_envs import UvEnv


log = structlog.get_logger(__name__)


class BaseSphinx(BaseBuilder):
    """Common parent for all Sphinx-driven builders."""

    sphinx_doctrees_dir = "_build/doctrees"

    # Output directory relative to ``$READTHEDOCS_OUTPUT`` (e.g. "html").
    relative_output_dir = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config_file = self.config.sphinx.configuration

        # Upstream maintains separate "host" and "container" paths because the
        # web side runs Python on the host while shell commands run inside the
        # container with a different mount point. The runner has a single
        # filesystem, so both names resolve to the same place — but we keep
        # both for line-by-line parity with the upstream backend until env-var
        # expansion lands.
        self.absolute_host_output_dir = os.path.join(
            self.project.checkout_path(self.version.slug),
            "_readthedocs/",
            self.relative_output_dir,
        )
        self.absolute_container_output_dir = os.path.join(
            "$READTHEDOCS_OUTPUT", self.relative_output_dir
        )

        # ``sphinx.configuration`` is required by config validation, so it's set
        # for any real build (upstream's conf.py auto-discovery was dropped
        # along with the implicit-config deprecation). Resolve it relative to the
        # checkout; a missing value is surfaced as ProjectConfigurationError by
        # show_conf() / build().
        if self.config_file:
            self.config_file = os.path.join(self.project_path, self.config_file)

    def get_language(self, project):
        """Return a Sphinx-compatible locale code for ``project.language``."""
        language = project.language
        return OLD_LANGUAGES_CODE_MAPPING.get(language, language)

    def show_conf(self):
        """``cat`` the ``conf.py`` into the build log so the user can see it."""
        if not self.config_file:
            raise ProjectConfigurationError(ProjectConfigurationError.NOT_FOUND)

        if not os.path.exists(self.config_file):
            raise UserFileNotFound(
                message_id=UserFileNotFound.FILE_NOT_FOUND,
                format_values={
                    "filename": os.path.relpath(self.config_file, self.project_path),
                },
            )

        self.run(
            "cat",
            os.path.relpath(self.config_file, self.project_path),
            cwd=self.project_path,
        )

    def build(self):
        project = self.project
        build_command = [
            *self.get_sphinx_cmd(),
            "-T",
        ]
        if self.config.sphinx.fail_on_warning:
            build_command.extend(["-W", "--keep-going"])
        language = self.get_language(project)

        if self.project.has_feature(Feature.BUILD_IN_PARALLEL):
            build_command.extend(["-j", "auto"])

        build_command.extend(
            [
                "-b",
                self.sphinx_builder,
                "-d",
                self.sphinx_doctrees_dir,
                "-D",
                f"language={language}",
                # SOURCEDIR is "." because we run from the conf.py's directory.
                ".",
                # OUTPUTDIR.
                self.absolute_container_output_dir,
            ]
        )
        cmd_ret = self.run(
            *build_command,
            bin_path=self.python_env.venv_bin(),
            cwd=os.path.dirname(self.config_file),
        )

        self._post_build()

        return cmd_ret.successful

    def get_sphinx_cmd(self):
        """Pick ``uv run sphinx-build`` or ``python -m sphinx`` based on the env."""
        if isinstance(self.python_env, UvEnv):
            # ``--no-sync``: the env was already synced by ``python.install``;
            # re-syncing here would reinstall (and fail without a lockfile).
            # ``--no-dev``: don't pull in dev dependency groups. Matches upstream.
            return ("uv", "run", "--no-sync", "--no-dev", "sphinx-build")

        return (
            self.python_env.venv_bin(filename="python"),
            "-m",
            "sphinx",
        )


class HtmlBuilder(BaseSphinx):
    relative_output_dir = "html"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sphinx_builder = "html"


class HtmlDirBuilder(HtmlBuilder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sphinx_builder = "dirhtml"


class SingleHtmlBuilder(HtmlBuilder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sphinx_builder = "singlehtml"


class LocalMediaBuilder(BaseSphinx):
    sphinx_builder = "singlehtml"
    relative_output_dir = "htmlzip"

    def _post_build(self):
        """Repackage the singlehtml output as ``<project>.zip``."""
        target_file = os.path.join(
            self.absolute_container_output_dir,
            # Upstream notes the name should arguably include the version slug
            # but kept it as project slug for backward compatibility.
            f"{self.project.slug}.zip",
        )

        # SECURITY CRITICAL: Advisory GHSA-hqwg-gjqw-h5wg.
        # Move the directory into a tmpdir so zip uses the rename as arcname.
        mktemp = self.run("mktemp", "--directory", record=False)
        tmp_dir = Path(mktemp.output.strip())
        dirname = f"{self.project.slug}-{self.version.slug}"
        self.run(
            "mv",
            self.absolute_container_output_dir,
            str(tmp_dir / dirname),
            cwd=self.project_path,
            record=False,
        )
        self.run(
            "mkdir",
            "--parents",
            self.absolute_container_output_dir,
            cwd=self.project_path,
            record=False,
        )
        self.run(
            "zip",
            "--recurse-paths",
            "--symlinks",
            target_file,
            dirname,
            cwd=str(tmp_dir),
            record=False,
        )


class EpubBuilder(BaseSphinx):
    sphinx_builder = "epub"
    relative_output_dir = "epub"

    def _post_build(self):
        """Move the produced ``.epub`` to a stable name; drop intermediates."""
        temp_epub_file = f"/tmp/{self.project.slug}-{self.version.slug}.epub"
        target_file = os.path.join(
            self.absolute_container_output_dir,
            f"{self.project.slug}.epub",
        )

        epub_sphinx_filepaths = glob(os.path.join(self.absolute_host_output_dir, "*.epub"))
        if epub_sphinx_filepaths:
            # Only one .epub is supported per version.
            epub_filepath = epub_sphinx_filepaths[0]

            self.run("mv", epub_filepath, temp_epub_file, cwd=self.project_path, record=False)
            self.run(
                "rm",
                "--recursive",
                self.absolute_container_output_dir,
                cwd=self.project_path,
                record=False,
            )
            self.run(
                "mkdir",
                "--parents",
                self.absolute_container_output_dir,
                cwd=self.project_path,
                record=False,
            )
            self.run("mv", temp_epub_file, target_file, cwd=self.project_path, record=False)


class LatexBuildCommandMixin:
    """Ignore LaTeX's exit code if it produced a PDF anyway."""

    def run(self):
        super().run()
        # Be optimistic: if LaTeX wrote an output file, treat the exit as success.
        if PDF_RE.search(self.output or ""):
            self.exit_code = 0


class LatexBuildCommand(LatexBuildCommandMixin, BuildCommand):
    """Local variant. Tests only — see the note on ``BuildEnvironment``."""


class DockerLatexBuildCommand(LatexBuildCommandMixin, DockerBuildCommand):
    """Production variant: runs inside the build container like every other command."""


# The environment decides how commands are executed, so it also decides which
# LaTeX variant to use. Keyed by ``BuildEnvironment.command_class``.
LATEX_COMMAND_CLASSES = {
    BuildCommand: LatexBuildCommand,
    DockerBuildCommand: DockerLatexBuildCommand,
}


class PdfBuilder(BaseSphinx):
    """Sphinx LaTeX → PDF builder."""

    relative_output_dir = "pdf"
    sphinx_builder = "latex"
    pdf_file_name = None

    def build(self):
        language = self.get_language(self.project)
        self.run(
            *self.get_sphinx_cmd(),
            "-T",
            "-b",
            self.sphinx_builder,
            "-d",
            self.sphinx_doctrees_dir,
            "-D",
            f"language={language}",
            ".",
            self.absolute_container_output_dir,
            cwd=os.path.dirname(self.config_file),
            bin_path=self.python_env.venv_bin(),
        )

        tex_files = glob(os.path.join(self.absolute_host_output_dir, "*.tex"))
        if not tex_files:
            raise BuildUserError(message_id=BuildUserError.TEX_FILE_NOT_FOUND)

        success = self._build_latexmk(self.project_path)

        self._post_build()
        return success

    def _build_latexmk(self, cwd):
        # Steps lifted from Sphinx >=1.6's generated Makefile.
        images = []
        for extension in ("png", "gif", "jpg", "jpeg"):
            images.extend(Path(self.absolute_host_output_dir).glob(f"*.{extension}"))

        # platex (Japanese) needs the bbox extracted from existing PDFs too.
        pdfs = []
        if self.project.language == "ja":
            pdfs = Path(self.absolute_host_output_dir).glob("*.pdf")

        for image in itertools.chain(images, pdfs):
            self.run(
                "extractbb",
                image.name,
                cwd=self.absolute_host_output_dir,
                record=False,
            )

        rcfile = "latexmkrc"
        if self.project.language == "ja":
            rcfile = "latexmkjarc"

        self.run("cat", rcfile, cwd=self.absolute_host_output_dir)

        cmd = [
            "latexmk",
            "-r",
            rcfile,
            "-pdfdvi" if self.project.language == "ja" else "-pdf",
            # ``-f``: keep going on errors. We still get a non-zero exit, but
            # LatexBuildCommand promotes it to 0 if a PDF was written.
            "-f",
            "-dvi-",
            "-ps-",
            f"-jobname={self.project.slug}",
            "-interaction=nonstopmode",
        ]

        cmd_ret = self.build_env.run_command_class(
            cls=LATEX_COMMAND_CLASSES[self.build_env.command_class],
            cmd=cmd,
            warn_only=True,
            cwd=self.absolute_host_output_dir,
        )

        self.pdf_file_name = f"{self.project.slug}.pdf"
        return cmd_ret.successful

    def _post_build(self):
        """Reduce the LaTeX output directory to a single ``<slug>.pdf``."""
        if not self.pdf_file_name:
            raise BuildUserError(BuildUserError.PDF_NOT_FOUND)

        temp_pdf_file = f"/tmp/{self.project.slug}-{self.version.slug}.pdf"
        target_file = os.path.join(self.absolute_container_output_dir, self.pdf_file_name)

        pdf_sphinx_filepath = os.path.join(self.absolute_container_output_dir, self.pdf_file_name)
        pdf_sphinx_filepath_host = os.path.join(self.absolute_host_output_dir, self.pdf_file_name)
        if os.path.exists(pdf_sphinx_filepath_host):
            self.run(
                "mv",
                pdf_sphinx_filepath,
                temp_pdf_file,
                cwd=self.project_path,
                record=False,
            )
            self.run(
                "rm",
                "-r",
                self.absolute_container_output_dir,
                cwd=self.project_path,
                record=False,
            )
            self.run(
                "mkdir",
                "-p",
                self.absolute_container_output_dir,
                cwd=self.project_path,
                record=False,
            )
            self.run("mv", temp_pdf_file, target_file, cwd=self.project_path, record=False)

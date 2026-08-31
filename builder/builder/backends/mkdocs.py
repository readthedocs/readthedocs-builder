"""
MkDocs backend.

Ported from ``readthedocs.doc_builder.backends.mkdocs``. Drops the unused
helpers (``get_absolute_static_url``, ``yaml_load_safely``, the custom
``SafeLoader``/``SafeDumper``, ``ProxyPythonName``) — they're only referenced
from upstream tests, never from the build pipeline itself.
"""

import os

import structlog

from builder.base import BaseBuilder
from builder.exceptions import UserFileNotFound
from builder.python_envs import UvEnv


log = structlog.get_logger(__name__)


class BaseMkdocs(BaseBuilder):
    """MkDocs builder."""

    DEFAULT_THEME_NAME = "mkdocs"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Absolute path to the user's mkdocs.yml.
        self.config_file = os.path.join(
            self.project_path,
            self.config.mkdocs.configuration,
        )

    def show_conf(self):
        """``cat`` the user's ``mkdocs.yml`` into the build log."""
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
        build_command = [
            *self.get_mkdocs_cmd(),
            self.builder,
            "--clean",
            "--site-dir",
            os.path.join("$READTHEDOCS_OUTPUT", "html"),
            "--config-file",
            os.path.relpath(self.config_file, self.project_path),
        ]

        if self.config.mkdocs.fail_on_warning:
            build_command.append("--strict")
        cmd_ret = self.run(
            *build_command,
            cwd=self.project_path,
            bin_path=self.python_env.venv_bin(),
        )
        return cmd_ret.successful

    def get_mkdocs_cmd(self):
        """Pick ``uv run mkdocs`` or ``python -m mkdocs`` based on the env."""
        if isinstance(self.python_env, UvEnv):
            # See ``SphinxBuilder.get_sphinx_cmd``: skip the implicit re-sync and
            # dev dependency groups. Matches upstream.
            return ("uv", "run", "--no-sync", "--no-dev", "mkdocs")

        return (
            self.python_env.venv_bin(filename="python"),
            "-m",
            "mkdocs",
        )


class MkdocsHTML(BaseMkdocs):
    builder = "build"
    build_dir = "_readthedocs/html"

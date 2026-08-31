"""
Base class for documentation builders.

Ported from ``readthedocs.doc_builder.base``.
"""

import os

import structlog


log = structlog.get_logger(__name__)


class BaseBuilder:
    """The base for documentation builders."""

    ignore_patterns = []

    def __init__(self, build_env, python_env):
        self.build_env = build_env
        self.python_env = python_env
        self.version = build_env.version
        self.project = build_env.project
        self.config = python_env.config if python_env else None
        self.project_path = self.project.checkout_path(self.version.slug)
        self.api_client = self.build_env.api_client

    def get_final_doctype(self):
        """Some builders may resolve a more specific doctype at build time."""
        return self.config.doctype

    def show_conf(self):
        """Show the configuration used for this builder."""

    def build(self):
        """Run the build. Subclasses must override."""
        raise NotImplementedError

    def _post_build(self):
        """Hook for post-build cleanup (zip, rename, etc)."""

    def docs_dir(self):
        """Find the user's docs source directory under ``project_path``."""
        for doc_dir_name in ["docs", "doc", "Doc", "book"]:
            possible_path = os.path.join(self.project_path, doc_dir_name)
            if os.path.exists(possible_path):
                return possible_path
        return self.project_path

    def run(self, *args, **kwargs):
        """Proxy to ``build_env.run``."""
        return self.build_env.run(*args, **kwargs)

"""
Lightweight wrappers around the Read the Docs API JSON payloads.

Replaces upstream's Django proxy-model approach
(``readthedocs.builds.models.APIVersion``,
``readthedocs.projects.models.APIProject``) with plain Python classes that
hold the API data as attributes and implement only the methods the build
runner actually calls.

The plain ``Build`` payload from the API is kept as a dict (matching upstream
behaviour); see ``builder.api_client.get_build``.
"""

import os
import re
from urllib.parse import urlparse

import structlog

from builder import settings
from builder.constants import EXTERNAL
from builder.constants import LATEST


log = structlog.get_logger(__name__)


def _repo_host(repo: str | None) -> str:
    """Return the host part of a Git URL (HTTPS or git@host:path SSH form)."""
    if not repo:
        return ""
    ssh_match = re.match(r"^[^@]+@([^:]+):", repo)
    if ssh_match:
        return ssh_match.group(1).lower()
    return urlparse(repo).netloc.lower()


class APIProject:
    """API-backed project. Fields populated from the JSON returned by the RTD API."""

    def __init__(self, **data):
        # Identifiers.
        self.pk = data.get("id") or data.get("pk")
        self.id = self.pk
        self.slug = data["slug"]
        self.name = data.get("name", "")
        self.language = data.get("language", "en")

        # Repository.
        self.repo = data.get("repo")
        self.repo_type = data.get("repo_type")
        self.default_branch = data.get("default_branch")
        self.default_version = data.get("default_version", LATEST)
        self.readthedocs_yaml_path = data.get("readthedocs_yaml_path")
        # Optional list of shell commands to run instead of ``git clone``.
        # Stored as JSON on the upstream model; comes through as a list (or None).
        self.git_checkout_command = data.get("git_checkout_command")

        # Permissions / auth. ``clone_token`` is a property in the upstream
        # Project model, re-declared as a plain attribute on APIProject.
        self.has_ssh_key_with_write_access = data.get("has_ssh_key_with_write_access", False)
        self.clone_token = data.get("clone_token")

        # Builds disabled for this project. The API computes it from
        # ``Project.objects.is_active`` (not just the ``skip`` field).
        self.skip = data.get("skip", False)

        # Feature flags & per-project env vars come back as structured fields.
        self._features = data.get("features", []) or []
        self._environment_variables = data.get("environment_variables", {}) or {}

        # Ad-free flag (inverse of ``show_advertising``).
        self.ad_free = not data.get("show_advertising", True)

        # Hold raw payload for debugging and forward-compatibility.
        self._raw = data

    # ---- Path helpers (mirror upstream layout under DOCROOT) ----

    @property
    def doc_path(self) -> str:
        return os.path.join(settings.DOCROOT, self.slug.replace("_", "-"))

    def checkout_path(self, version: str = LATEST) -> str:
        return os.path.join(self.doc_path, "checkouts", version)

    def artifact_path(self, type_: str, version: str = LATEST) -> str:
        return os.path.join(self.checkout_path(version=version), "_readthedocs", type_)

    # ---- Repository URL helpers ----

    @property
    def clean_repo(self) -> str | None:
        """Normalize the clone URL (rewrite ``http://github.com`` to HTTPS)."""
        if self.repo and self.repo.startswith("http://github.com"):
            return self.repo.replace("http://github.com", "https://github.com")
        return self.repo

    @property
    def is_github_project(self) -> bool:
        """Heuristic: clone URL points at github.com.

        Upstream consults the OAuth service registry; we don't have the DB
        models to do that, so URL host matching is the v1 fallback.
        """
        return _repo_host(self.repo) == "github.com"

    @property
    def is_gitlab_project(self) -> bool:
        return _repo_host(self.repo) == "gitlab.com"

    # ---- Feature & env helpers ----

    def has_feature(self, feature_id: str) -> bool:
        return feature_id in self._features

    def environment_variables(self, *, public_only: bool = True) -> dict:
        return {
            name: spec["value"]
            for name, spec in self._environment_variables.items()
            if spec.get("public") or not public_only
        }

    # ---- VCS dispatch ----

    def vcs_class(self):
        """Return the VCS backend class for this project. Only ``git`` is supported."""
        if self.repo_type and self.repo_type != "git":
            return None
        from builder.vcs import Backend

        return Backend

    def vcs_repo(self, environment, version):
        """Instantiate the VCS backend bound to ``version`` and ``environment``."""
        backend = self.vcs_class()
        if not backend:
            return None
        return backend(
            self,
            version=version,
            environment=environment,
            use_token=bool(self.clone_token),
        )

    def __repr__(self) -> str:
        return f"APIProject(slug={self.slug!r}, pk={self.pk!r})"


class APIVersion:
    """API-backed version. Fields populated from the JSON returned by the RTD API."""

    def __init__(self, **data):
        self.pk = data.get("id") or data.get("pk")
        self.id = self.pk
        self.slug = data["slug"]
        self.verbose_name = data.get("verbose_name", "")
        self.identifier = data.get("identifier", "")
        # ``git_identifier`` is a model property upstream, re-declared here as
        # a plain attribute (the API returns it directly).
        self.git_identifier = data.get("git_identifier")
        self.type = data.get("type", "branch")
        self.machine = data.get("machine", False)
        self.documentation_type = data.get("documentation_type")
        self.canonical_url = data.get("canonical_url")
        # ``build_data`` is set by the builder after parsing readthedocs-build.yaml
        # from the user's repo; it's pushed back to the API on completion.
        self.build_data = data.get("build_data")

        # Nested project. Always present when the version is fetched as part
        # of the build payload via /api/v2/version/<pk>/.
        project_data = data.get("project")
        self.project = APIProject(**project_data) if project_data else None

        self._raw = data

    # ---- Computed properties ----

    @property
    def is_external(self) -> bool:
        return self.type == EXTERNAL

    @property
    def is_machine_latest(self) -> bool:
        return self.machine and self.slug == LATEST

    def get_storage_path(self, media_type: str, version_slug: str | None = None) -> str:
        """
        Remote storage path for an artifact of this version.

        Layout (matches upstream ``Version.get_storage_path``):

        - internal: ``<media_type>/<project_slug>/<version_slug>``
        - external (PR/MR): ``external/<media_type>/<project_slug>/<version_slug>``

        ``version_slug`` overrides ``self.slug`` for cleaning old versions
        whose slug has since changed.
        """
        slug = version_slug or self.slug
        parts = [media_type, self.project.slug, slug]
        if self.is_external:
            parts = ["external"] + parts
        return "/".join(parts)

    def __repr__(self) -> str:
        return f"APIVersion(slug={self.slug!r}, pk={self.pk!r}, type={self.type!r})"

"""
Remote ``git fetch`` refspec for a version.

Shared by the runner (``builder/``) and the worker's bootstrap sparse-clone
(``worker/``) so both fetch the *same* ref. This matters most for tags and
external (PR/MR) versions, whose ``identifier`` is a commit hash rather than a
name — ``git clone -b <hash>`` fails, so the ref has to be fetched explicitly.

Ported from readthedocs.org
``readthedocs/vcs_support/backends/git.py:get_remote_fetch_refspec`` to keep the
two implementations from diverging.
"""

import structlog


log = structlog.get_logger(__name__)

# Version types. Mirror readthedocs.org ``builds/constants.py``.
BRANCH = "branch"
TAG = "tag"
EXTERNAL = "external"
STABLE = "stable"

# PR/MR fetch patterns. Mirror readthedocs.org ``projects/constants.py``.
GITHUB_PR_PULL_PATTERN = "pull/{id}/head:external-{id}"
GITLAB_MR_PULL_PATTERN = "merge-requests/{id}/head:external-{id}"


def get_remote_fetch_refspec(
    *,
    version_type,
    verbose_name,
    identifier,
    machine,
    slug,
    is_github,
    is_gitlab,
):
    """
    Build the ``git fetch`` refspec for a version, or ``None`` if undecidable.

    Branches/tags use ``refs/heads/...`` / ``refs/tags/...``; machine-created
    ``stable`` falls back to the commit hash; external (PR/MR) versions use the
    provider's pull/merge refspec. See ``git help fetch`` for the ``<refspec>``
    grammar.
    """
    branch_ref = "refs/heads/{branch}:refs/remotes/origin/{branch}"
    tag_ref = "refs/tags/{tag}:refs/tags/{tag}"

    if version_type == BRANCH:
        branch = verbose_name
        # For latest/stable (machine-created) the identifier holds the branch name.
        if machine:
            branch = identifier
            if not branch:
                log.error(
                    "Machine created version without a branch name.",
                    version_slug=slug,
                )
                return None
        return branch_ref.format(branch=branch)

    if version_type == TAG:
        tag = verbose_name
        if machine:
            # ``stable`` points at an exact commit; ``latest`` at the tag name.
            if slug == STABLE:
                if not identifier:
                    log.error("'stable' version without a commit hash.")
                return identifier
            tag = identifier
        return tag_ref.format(tag=tag)

    if version_type == EXTERNAL:
        if is_github:
            return GITHUB_PR_PULL_PATTERN.format(id=verbose_name)
        if is_gitlab:
            return GITLAB_MR_PULL_PATTERN.format(id=verbose_name)
        log.warning(
            "Asked to do an external build for a Git provider that does not "
            "support fetching a pr/mr refspec.",
            version_slug=slug,
        )

    return None

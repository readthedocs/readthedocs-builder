"""
Parse ``git ls-remote`` output and validate reserved version names.

Shared by the runner (``builder/vcs.py:lsremote``) and the worker, which runs a
host-side ``git ls-remote`` before the build to sync tags/branches into the
database (and to fail early on duplicate reserved versions). Keeping the parsing
here stops the two from diverging.

Ported from readthedocs.org ``vcs_support/backends/git.py:lsremote`` and
``projects/tasks/mixins.py:validate_duplicate_reserved_versions``.
"""

from collections import Counter


# Reserved version names a user may not duplicate across branches/tags. Mirror
# readthedocs.org ``builds/constants.py`` (``STABLE``/``LATEST`` verbose names).
STABLE_VERBOSE_NAME = "stable"
LATEST_VERBOSE_NAME = "latest"


def parse_lsremote(stdout: str):
    """
    Parse ``git ls-remote --heads --tags`` output into ``(branches, tags)``.

    Each entry is an ``(identifier, verbose_name)`` tuple, matching the identity
    convention the API expects:

    - branch: ``(branch_name, branch_name)`` — the branch name is the identifier.
    - tag: ``(commit_hash, tag_name)`` — annotated tags (listed as both ``<tag>``
      and ``<tag>^{}``) resolve to the dereferenced commit.
    """
    branches = []
    all_tags = {}
    light_tags = {}
    for line in stdout.splitlines():
        try:
            commit, ref = line.split(maxsplit=1)
        except ValueError:
            continue
        if ref.startswith("refs/heads/"):
            branch = ref.replace("refs/heads/", "", 1)
            branches.append((branch, branch))
        elif ref.startswith("refs/tags/"):
            tag = ref.replace("refs/tags/", "", 1)
            if tag.endswith("^{}"):
                light_tags[tag[:-3]] = commit
            else:
                all_tags[tag] = commit

    all_tags.update(light_tags)
    tags = [(commit, tag) for tag, commit in all_tags.items()]
    return branches, tags


def find_duplicate_reserved_versions(branches, tags):
    """
    Return the reserved names (``latest``/``stable``) that appear more than once.

    ``branches`` and ``tags`` are ``(identifier, verbose_name)`` sequences (as
    returned by :func:`parse_lsremote`). A user may not have a branch and a tag
    both named ``latest`` or both named ``stable`` — upstream fails the build in
    that case.
    """
    names = [name for _, name in branches] + [name for _, name in tags]
    counter = Counter(names)
    return {name for name in (STABLE_VERBOSE_NAME, LATEST_VERBOSE_NAME) if counter[name] > 1}

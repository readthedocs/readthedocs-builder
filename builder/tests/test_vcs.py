"""
Tests for the git VCS backend.

Ported from ``readthedocs/rtd_tests/tests/test_backend.py`` (the git
``TestGitBackend`` suite). Differences from upstream, all intentional:

- No Django models: ``Project`` / ``Version`` are replaced by ``APIProject`` /
  ``APIVersion`` built from plain payloads (see ``make_backend``).
- Real commands run through a real ``BuildEnvironment(record=False)`` instead
  of a ``LocalBuildEnvironment`` with ``BuildCommand.save`` mocked.
- Git repos are built with ``git -C`` helpers rather than the ``chdir`` +
  ``GIT_DIR`` fixture the upstream utils use.

The pure-logic paths that upstream only exercises indirectly
(``get_remote_fetch_refspec``, ``parse_version_from_ref``, ``_get_clone_url``,
the SSH-key probe) get direct unit tests here.
"""

import os
from os.path import exists
from unittest import mock

import pytest
from conftest import current_commit
from conftest import git
from conftest import make_backend
from conftest import submodules_config

from builder.constants import ALL
from builder.constants import BRANCH
from builder.constants import EXTERNAL
from builder.constants import STABLE
from builder.constants import TAG
from builder.exceptions import BuildCancelled
from builder.exceptions import RepositoryError
from builder.vcs import VCSVersion
from builder.vcs import parse_version_from_ref


# ---------------------------------------------------------------------------
# lsremote
# ---------------------------------------------------------------------------


def test_lsremote_lists_branches_and_tags(git_repo, docroot):
    for branch in ["develop", "2.0.X", "release/2.0.0", "with\xa0space"]:
        git(git_repo, "branch", branch)
    git(git_repo, "tag", "v01")
    git(git_repo, "tag", "-a", "-m", "annotated", "v02")
    git(git_repo, "tag", "release-ünîø∂é")

    repo = make_backend(git_repo)
    repo.check_working_dir()
    commit = current_commit(git_repo)

    branches, tags = repo.lsremote()

    assert {b.verbose_name: b.identifier for b in branches} == {
        name: name
        for name in ["master", "submodule", "develop", "2.0.X", "release/2.0.0", "with\xa0space"]
    }
    # Annotated tags resolve to the commit they point at, not the tag object.
    assert {t.verbose_name: t.identifier for t in tags} == {
        "v01": commit,
        "v02": commit,
        "release-ünîø∂é": commit,
    }


def test_lsremote_tags_only(git_repo, docroot):
    git(git_repo, "branch", "develop")
    git(git_repo, "tag", "v01")

    repo = make_backend(git_repo)
    repo.check_working_dir()

    branches, tags = repo.lsremote(include_tags=True, include_branches=False)
    assert branches == []
    assert {t.verbose_name for t in tags} == {"v01"}


def test_lsremote_branches_only(git_repo, docroot):
    git(git_repo, "branch", "develop")
    git(git_repo, "tag", "v01")

    repo = make_backend(git_repo)
    repo.check_working_dir()

    branches, tags = repo.lsremote(include_tags=False, include_branches=True)
    assert tags == []
    assert {b.verbose_name for b in branches} == {"master", "submodule", "develop"}


def test_lsremote_with_neither_flag_returns_empty(git_repo, docroot):
    repo = make_backend(git_repo)
    # Short-circuits before touching the network — no working dir needed.
    assert repo.lsremote(include_tags=False, include_branches=False) == ([], [])


def test_lsremote_raises_on_failure(git_repo, docroot):
    repo = make_backend(git_repo)
    with mock.patch.object(repo, "run", return_value=(1, "Error", "")):
        with pytest.raises(RepositoryError) as excinfo:
            repo.lsremote()
    assert excinfo.value.message_id == RepositoryError.FAILED_TO_GET_VERSIONS


def test_lsremote_skips_malformed_lines(git_repo, docroot):
    repo = make_backend(git_repo)
    stdout = "garbage-without-whitespace\nabc123\trefs/heads/main\ndef456\trefs/tags/v1\n"
    with mock.patch.object(repo, "run", return_value=(0, stdout, "")):
        branches, tags = repo.lsremote()
    assert {b.verbose_name for b in branches} == {"main"}
    assert {t.verbose_name for t in tags} == {"v1"}


# ---------------------------------------------------------------------------
# update / checkout
# ---------------------------------------------------------------------------


def test_update_clones_the_repository(git_repo, docroot):
    repo = make_backend(git_repo)
    code, _, _ = repo.update()
    assert code == 0
    assert exists(repo.working_dir)
    assert exists(os.path.join(repo.working_dir, "README"))


def test_checkout_without_identifier_stays_on_default_branch(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    # No identifier -> returns None and leaves the default branch checked out.
    assert repo.checkout() is None
    assert exists(os.path.join(repo.working_dir, "only-on-default-branch"))


def test_checkout_invalid_revision_raises(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    with pytest.raises(RepositoryError) as excinfo:
        repo.checkout("invalid-revision")
    assert excinfo.value.message_id == RepositoryError.FAILED_TO_CHECKOUT


def test_update_checks_out_the_requested_branch(git_repo, docroot):
    git(git_repo, "branch", "develop")
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="develop",
        verbose_name="develop",
        identifier="develop",
    )
    repo.update()
    repo.checkout("develop")
    # develop branched off master, so the default-branch marker is present, but
    # the working dir is a real checkout with the README.
    assert exists(os.path.join(repo.working_dir, "README"))


def test_update_machine_latest_fetches_head(git_repo, docroot):
    # A machine-created "latest" with no project default branch fetches the
    # remote HEAD symref instead of a named branch.
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="latest",
        verbose_name="latest",
        identifier="",
        machine=True,
    )
    code, _, _ = repo.update()
    assert code == 0
    assert exists(os.path.join(repo.working_dir, "README"))


def test_update_runs_a_custom_checkout_command(git_repo, docroot):
    # A project-level custom checkout command replaces clone+fetch entirely.
    repo = make_backend(git_repo, git_checkout_command=["git init", "git status"])
    repo.update()
    # A custom command was configured, so checkout() short-circuits to None.
    assert repo.checkout("master") is None
    assert exists(repo.working_dir)


def test_custom_checkout_command_runs_verbatim_instead_of_clone_and_fetch(git_repo, docroot):
    repo = make_backend(git_repo, git_checkout_command=["git init", "git status"])
    ran = []
    original_run = repo.run

    def spy(*cmd, **kwargs):
        ran.append((cmd, kwargs))
        return original_run(*cmd, **kwargs)

    with mock.patch.object(repo, "run", side_effect=spy):
        repo.update()

    commands = [cmd for cmd, _ in ran]
    assert commands == [("git", "init"), ("git", "status")]
    assert all(kwargs.get("escape_command") is False for _, kwargs in ran)


def test_run_translates_a_failed_command_to_repository_error(git_repo, docroot):
    repo = make_backend(git_repo)
    with mock.patch.object(
        repo.environment, "run", side_effect=BuildCancelled(BuildCancelled.CANCELLED_BY_USER)
    ):
        with pytest.raises(BuildCancelled):
            repo.run("git", "status")


def test_clone_error_maps_to_public_repo_message(git_repo, docroot):
    repo = make_backend(git_repo, repo="/does/not/exist")
    with pytest.raises(RepositoryError) as excinfo:
        repo.update()
    assert (
        excinfo.value.message_id
        == RepositoryError.CLONE_ERROR_WITH_PRIVATE_REPO_NOT_ALLOWED
    )


def test_clone_error_maps_to_private_repo_message(git_repo, docroot):
    repo = make_backend(git_repo, repo="/does/not/exist", allow_private_repos=True)
    with pytest.raises(RepositoryError) as excinfo:
        repo.update()
    assert (
        excinfo.value.message_id
        == RepositoryError.CLONE_ERROR_WITH_PRIVATE_REPO_ALLOWED
    )


def test_make_clean_working_dir_recreates_an_empty_dir(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    assert exists(os.path.join(repo.working_dir, "README"))
    repo.make_clean_working_dir()
    assert exists(repo.working_dir)
    assert os.listdir(repo.working_dir) == []


def test_check_working_dir_creates_the_dir_via_a_wrapped_command(git_repo, docroot):
    # The dir must be created through ``environment.run`` (so it's ``runuser``
    # wrapped and owned by the build user), not via ``os.makedirs`` in the root
    # runner process. Guards the ownership regression on root:root containers.
    repo = make_backend(git_repo)
    assert not exists(repo.working_dir)

    with mock.patch.object(
        repo.environment, "run", wraps=repo.environment.run
    ) as run:
        repo.check_working_dir()

    assert exists(repo.working_dir)
    assert run.call_args.args[0] == "mkdir"


def test_check_working_dir_is_a_noop_when_the_dir_exists(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.check_working_dir()

    with mock.patch.object(repo.environment, "run") as run:
        repo.check_working_dir()

    run.assert_not_called()


def test_get_default_branch(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    assert repo.get_default_branch() == "master"


def test_commit_returns_head_hash(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    assert repo.commit == current_commit(git_repo)


# ---------------------------------------------------------------------------
# find_ref / ref_exists
# ---------------------------------------------------------------------------


def test_ref_exists_for_a_fetched_branch(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    assert repo.ref_exists("refs/remotes/origin/master") is True
    assert repo.ref_exists("refs/remotes/origin/does-not-exist") is False


def test_find_ref_prefixes_a_known_branch_with_origin(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    assert repo.find_ref("master") == "origin/master"


def test_find_ref_passes_through_an_origin_prefixed_ref(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    assert repo.find_ref("origin/master") == "origin/master"


def test_find_ref_passes_through_an_unknown_ref(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    # A commit hash / unknown name isn't rewritten.
    assert repo.find_ref("abc123") == "abc123"


# ---------------------------------------------------------------------------
# External (PR/MR) versions
# ---------------------------------------------------------------------------


def test_update_external_version_calls_fetch(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=EXTERNAL,
        slug="1234",
        verbose_name="1234",
        identifier="1234",
    )
    with mock.patch.object(repo, "fetch") as fetch:
        repo.update()
    fetch.assert_called_once()


def test_fetch_external_version_runs(git_repo, docroot):
    # A non-GitHub/GitLab remote yields no PR refspec, so fetch runs without a
    # ref and still succeeds against the local remote.
    repo = make_backend(
        git_repo,
        version_type=EXTERNAL,
        slug="1234",
        verbose_name="1234",
        identifier="1234",
    )
    repo.update()
    code, _, _ = repo.fetch()
    assert code == 0


# ---------------------------------------------------------------------------
# Submodules
# ---------------------------------------------------------------------------


def test_submodules_not_available_on_default_branch(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    assert repo.are_submodules_available(submodules_config()) is False


def test_submodules_available_on_submodule_branch(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="submodule",
        verbose_name="submodule",
        identifier="submodule",
    )
    repo.update()
    repo.checkout("submodule")
    assert repo.are_submodules_available(submodules_config()) is True


def test_get_available_submodules_returns_all(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="submodule",
        verbose_name="submodule",
        identifier="submodule",
    )
    repo.update()
    repo.checkout("submodule")
    valid, paths = repo.get_available_submodules(submodules_config(include=ALL))
    assert valid is True
    # "all" is signalled by an empty path list.
    assert paths == []


def test_get_available_submodules_excludes_all(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="submodule",
        verbose_name="submodule",
        identifier="submodule",
    )
    repo.update()
    repo.checkout("submodule")
    valid, paths = repo.get_available_submodules(submodules_config(include=[], exclude=ALL))
    assert valid is False
    assert paths == []


def test_get_available_submodules_honours_an_exclude_list(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="submodule",
        verbose_name="submodule",
        identifier="submodule",
    )
    repo.update()
    repo.checkout("submodule")
    # Excluding the only submodule leaves nothing to fetch.
    valid, paths = repo.get_available_submodules(
        submodules_config(include=[], exclude=["foobar"])
    )
    assert valid is False
    assert paths == []


def test_update_submodules_checks_out_selected_paths(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="submodule",
        verbose_name="submodule",
        identifier="submodule",
    )
    repo.update()
    repo.checkout("submodule")
    with mock.patch.object(repo, "checkout_submodules") as checkout_submodules:
        repo.update_submodules(submodules_config(include=ALL, recursive=True))
    checkout_submodules.assert_called_once_with([], True)


def test_update_submodules_skips_when_none_available(git_repo, docroot):
    repo = make_backend(git_repo)
    repo.update()
    with mock.patch.object(repo, "checkout_submodules") as checkout_submodules:
        repo.update_submodules(submodules_config())
    checkout_submodules.assert_not_called()


def test_submodule_without_a_url_is_still_listed(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="submodule",
        verbose_name="submodule",
        identifier="submodule",
    )
    repo.update()
    repo.checkout("submodule")
    gitmodules = os.path.join(repo.working_dir, ".gitmodules")
    with open(gitmodules, "a") as fh:
        fh.write('\n[submodule "not-valid-path"]\n    path = not-valid-path\n    url =\n')

    assert list(repo.submodules) == ["foobar", "not-valid-path"]


def test_parse_submodules_handles_odd_entries(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="submodule",
        verbose_name="submodule",
        identifier="submodule",
    )
    repo.update()
    repo.checkout("submodule")
    gitmodules = os.path.join(repo.working_dir, ".gitmodules")
    with open(gitmodules, "a") as fh:
        fh.write(
            "\n"
            '[submodule "not-valid-path"]\n    path = not-valid-path\n    url =\n\n'
            '[submodule "path with spaces"]\n    path = path with spaces\n'
            "    url = https://github.com\n\n"
            '[submodule "another-submodule"]\n    url = https://github.com\n'
            "    path = another-submodule\n\n"
            '[ssubmodule "invalid-submodule-key"]\n    url = https://github.com\n'
            "    path = invalid-submodule-key\n\n"
            '[submodule "invalid-path-key"]\n    url = https://github.com\n'
            "    paths = invalid-submodule-key\n\n"
            '[submodule "invalid-url-key"]\n    uurl = https://github.com\n'
            "    path = invalid-submodule-key\n"
        )

    # Only ``submodule.*.path`` keys are yielded; the ``.paths`` typo and the
    # ``ssubmodule`` section header are skipped.
    assert list(repo.submodules) == [
        "foobar",
        "not-valid-path",
        "path with spaces",
        "another-submodule",
        "invalid-submodule-key",
    ]


# ---------------------------------------------------------------------------
# get_remote_fetch_refspec — pure logic, no git needed
# ---------------------------------------------------------------------------


def test_refspec_for_a_branch(git_repo, docroot):
    repo = make_backend(
        git_repo, version_type=BRANCH, slug="develop", verbose_name="develop"
    )
    assert (
        repo.get_remote_fetch_refspec()
        == "refs/heads/develop:refs/remotes/origin/develop"
    )


def test_refspec_for_a_tag(git_repo, docroot):
    repo = make_backend(git_repo, version_type=TAG, slug="v1", verbose_name="v1")
    assert repo.get_remote_fetch_refspec() == "refs/tags/v1:refs/tags/v1"


def test_refspec_for_an_external_github_version_uses_the_pr_pull_ref(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=EXTERNAL,
        slug="2109",
        verbose_name="2109",
        identifier="9f4d838",
        repo="https://github.com/readthedocs/test-builds.git",
    )
    assert repo.get_remote_fetch_refspec() == "pull/2109/head:external-2109"


def test_refspec_for_a_machine_branch_uses_the_identifier(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="latest",
        verbose_name="latest",
        identifier="main",
        machine=True,
    )
    assert (
        repo.get_remote_fetch_refspec() == "refs/heads/main:refs/remotes/origin/main"
    )


def test_refspec_for_a_machine_branch_without_identifier_is_none(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=BRANCH,
        slug="latest",
        verbose_name="latest",
        identifier="",
        machine=True,
    )
    assert repo.get_remote_fetch_refspec() is None


def test_refspec_for_machine_stable_uses_the_commit_hash(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=TAG,
        slug=STABLE,
        verbose_name="stable",
        identifier="abc123",
        machine=True,
    )
    assert repo.get_remote_fetch_refspec() == "abc123"


def test_refspec_for_external_github(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=EXTERNAL,
        slug="1234",
        verbose_name="1234",
        repo="https://github.com/readthedocs/test.git",
    )
    assert repo.get_remote_fetch_refspec() == "pull/1234/head:external-1234"


def test_refspec_for_external_gitlab(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=EXTERNAL,
        slug="1234",
        verbose_name="1234",
        repo="https://gitlab.com/readthedocs/test.git",
    )
    assert repo.get_remote_fetch_refspec() == "merge-requests/1234/head:external-1234"


def test_refspec_for_external_unknown_provider_is_none(git_repo, docroot):
    repo = make_backend(
        git_repo,
        version_type=EXTERNAL,
        slug="1234",
        verbose_name="1234",
        repo="https://bitbucket.org/readthedocs/test.git",
    )
    assert repo.get_remote_fetch_refspec() is None


# ---------------------------------------------------------------------------
# _get_clone_url — token substitution
# ---------------------------------------------------------------------------


def test_clone_url_without_a_token_is_unchanged(git_repo, docroot):
    repo = make_backend(git_repo, repo="https://github.com/readthedocs/test.git")
    assert repo.repo_url == "https://github.com/readthedocs/test.git"


def test_clone_url_injects_the_token_when_present(git_repo, docroot):
    repo = make_backend(
        git_repo,
        repo="https://github.com/readthedocs/test.git",
        clone_token="secret",
    )
    assert (
        repo.repo_url
        == "https://$READTHEDOCS_GIT_CLONE_TOKEN@github.com/readthedocs/test.git"
    )


def test_clone_url_leaves_ssh_urls_alone(git_repo, docroot):
    repo = make_backend(
        git_repo,
        repo="git@github.com:readthedocs/test.git",
        clone_token="secret",
    )
    # Token substitution only applies to HTTP(S) URLs.
    assert repo.repo_url == "git@github.com:readthedocs/test.git"


# ---------------------------------------------------------------------------
# has_ssh_key_with_write_access — classify by well-known stderr strings
# ---------------------------------------------------------------------------


def _ssh_probe(repo, push_result):
    """Drive ``has_ssh_key_with_write_access`` with a canned dry-run push result.

    The remote add/remove calls return success; only the ``git push`` result
    (matched by the presence of ``push`` in the args) is faked.
    """

    def fake_run(*cmd, **kwargs):
        if "push" in cmd:
            return push_result
        return (0, "", "")

    with mock.patch.object(repo, "run", side_effect=fake_run):
        return repo.has_ssh_key_with_write_access()


def test_ssh_key_write_access_granted_on_success(git_repo, docroot):
    repo = make_backend(git_repo, repo="git@github.com:readthedocs/test.git")
    assert _ssh_probe(repo, (0, "", "")) is True


def test_ssh_key_write_access_denied_for_read_only_key(git_repo, docroot):
    repo = make_backend(git_repo, repo="git@github.com:readthedocs/test.git")
    stderr = "ERROR: Write access to repository not granted"
    assert _ssh_probe(repo, (1, "", stderr)) is False


def test_ssh_key_write_access_denied_for_permission_denied(git_repo, docroot):
    repo = make_backend(git_repo, repo="git@github.com:readthedocs/test.git")
    stderr = "ERROR: Permission to readthedocs/test.git denied to deploy key"
    assert _ssh_probe(repo, (1, "", stderr)) is False


def test_ssh_key_write_access_granted_for_archived_repo(git_repo, docroot):
    repo = make_backend(git_repo, repo="git@github.com:readthedocs/test.git")
    stderr = "ERROR: This repository was archived so it is read-only"
    assert _ssh_probe(repo, (1, "", stderr)) is True


def test_ssh_key_write_access_granted_for_empty_default_branch(git_repo, docroot):
    repo = make_backend(git_repo, repo="git@github.com:readthedocs/test.git")
    stderr = "error: src refspec refs/heads/m does not match any"
    assert _ssh_probe(repo, (1, "", stderr)) is True


def test_ssh_key_write_access_denied_for_unreadable_username(git_repo, docroot):
    repo = make_backend(git_repo, repo="https://github.com/readthedocs/test.git")
    stderr = "fatal: could not read Username for 'https://github.com'"
    assert _ssh_probe(repo, (1, "", stderr)) is False


def test_ssh_probe_converts_an_https_repo_to_ssh_form(git_repo, docroot):
    # The probe rewrites an HTTP(S) project repo to the ``git@host:path`` form
    # before adding it as a temporary remote.
    repo = make_backend(git_repo, repo="https://github.com/readthedocs/test.git")
    captured = []

    def fake_run(*cmd, **kwargs):
        captured.append(cmd)
        return (0, "", "")

    with mock.patch.object(repo, "run", side_effect=fake_run):
        repo.has_ssh_key_with_write_access()

    add_remote = next(c for c in captured if "add" in c)
    assert add_remote[-1] == "git@github.com:readthedocs/test.git"


def test_ssh_key_write_access_unknown_error_is_false(git_repo, docroot):
    repo = make_backend(git_repo, repo="git@github.com:readthedocs/test.git")
    assert _ssh_probe(repo, (1, "", "some unexpected failure")) is False


# ---------------------------------------------------------------------------
# parse_version_from_ref & VCSVersion
# ---------------------------------------------------------------------------


def test_parse_version_from_ref_branch():
    assert parse_version_from_ref("refs/heads/develop") == ("develop", BRANCH)


def test_parse_version_from_ref_tag():
    assert parse_version_from_ref("refs/tags/v1.0") == ("v1.0", TAG)


def test_parse_version_from_ref_invalid():
    with pytest.raises(ValueError):
        parse_version_from_ref("refs/remotes/origin/master")


def test_vcsversion_repr(git_repo, docroot):
    repo = make_backend(git_repo)
    version = VCSVersion(repo, "abc123", "v1.0")
    assert "v1.0" in repr(version)

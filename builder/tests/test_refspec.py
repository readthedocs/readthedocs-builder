"""Unit tests for the shared ``get_remote_fetch_refspec``."""

import pytest

from builder.refspec import get_remote_fetch_refspec


def refspec(**overrides):
    """Call the function with valid defaults, overriding only what a case needs."""
    kwargs = {
        "version_type": "branch",
        "verbose_name": "main",
        "identifier": "main",
        "machine": False,
        "slug": "main",
        "is_github": True,
        "is_gitlab": False,
    }
    kwargs.update(overrides)
    return get_remote_fetch_refspec(**kwargs)


def test_branch_uses_the_verbose_name():
    assert (
        refspec(version_type="branch", verbose_name="develop")
        == "refs/heads/develop:refs/remotes/origin/develop"
    )


def test_machine_branch_uses_the_identifier():
    # ``latest`` without an explicit default branch: identifier holds the branch.
    assert (
        refspec(version_type="branch", verbose_name="latest", identifier="main", machine=True)
        == "refs/heads/main:refs/remotes/origin/main"
    )


def test_machine_branch_without_identifier_is_none():
    assert (
        refspec(version_type="branch", verbose_name="latest", identifier="", machine=True) is None
    )


def test_tag_uses_the_verbose_name():
    assert refspec(version_type="tag", verbose_name="v1") == "refs/tags/v1:refs/tags/v1"


def test_machine_latest_tag_uses_the_identifier():
    # ``latest`` pointing at a tag: the identifier holds the tag name. This is
    # the branch neither consumer's tests exercise.
    assert (
        refspec(
            version_type="tag",
            slug="latest",
            verbose_name="latest",
            identifier="v2",
            machine=True,
        )
        == "refs/tags/v2:refs/tags/v2"
    )


def test_machine_stable_tag_uses_the_commit_hash():
    assert (
        refspec(
            version_type="tag",
            slug="stable",
            verbose_name="stable",
            identifier="abc123",
            machine=True,
        )
        == "abc123"
    )


def test_machine_stable_tag_without_identifier_returns_empty():
    # Logged as an error upstream, but still returns the (empty) identifier.
    assert (
        refspec(
            version_type="tag", slug="stable", verbose_name="stable", identifier="", machine=True
        )
        == ""
    )


def test_external_github_uses_the_pull_ref():
    assert (
        refspec(version_type="external", verbose_name="2109", is_github=True, is_gitlab=False)
        == "pull/2109/head:external-2109"
    )


def test_external_gitlab_uses_the_merge_request_ref():
    assert (
        refspec(version_type="external", verbose_name="42", is_github=False, is_gitlab=True)
        == "merge-requests/42/head:external-42"
    )


def test_external_unknown_provider_is_none():
    assert (
        refspec(version_type="external", verbose_name="7", is_github=False, is_gitlab=False) is None
    )


def test_unknown_version_type_is_none():
    assert refspec(version_type="unknown") is None

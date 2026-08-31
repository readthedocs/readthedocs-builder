"""Unit tests for the shared ``git ls-remote`` parsing + reserved-name check."""

from builder.lsremote import find_duplicate_reserved_versions
from builder.lsremote import parse_lsremote


def test_parse_lsremote_branches_use_the_name_as_identifier():
    out = "aaa\trefs/heads/main\nbbb\trefs/heads/feature-x\n"
    branches, tags = parse_lsremote(out)
    assert branches == [("main", "main"), ("feature-x", "feature-x")]
    assert tags == []


def test_parse_lsremote_lightweight_tag_uses_the_listed_commit():
    branches, tags = parse_lsremote("ccc\trefs/tags/v1.0\n")
    assert tags == [("ccc", "v1.0")]
    assert branches == []


def test_parse_lsremote_annotated_tag_dereferences_to_the_commit():
    # An annotated tag is listed twice: the tag object, then ``^{}`` -> commit.
    out = "tagobj\trefs/tags/v2.0\ncommit\trefs/tags/v2.0^{}\n"
    _, tags = parse_lsremote(out)
    assert tags == [("commit", "v2.0")]


def test_parse_lsremote_skips_malformed_lines():
    out = "aaa\trefs/heads/main\ngarbage-without-a-tab\n\n"
    branches, tags = parse_lsremote(out)
    assert branches == [("main", "main")]
    assert tags == []


def test_find_duplicate_reserved_versions_flags_stable_and_latest():
    branches = [("main", "main"), ("stable", "stable")]
    tags = [("abc", "stable"), ("def", "latest"), ("ghi", "latest")]
    assert find_duplicate_reserved_versions(branches, tags) == {"stable", "latest"}


def test_find_duplicate_reserved_versions_ignores_single_reserved_names():
    branches = [("main", "main"), ("stable", "stable")]
    tags = [("abc", "v1"), ("def", "latest")]
    assert find_duplicate_reserved_versions(branches, tags) == set()

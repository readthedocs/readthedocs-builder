"""
Tests for the build runner's lifecycle dispatch.

The runner orchestrates the director. The branch worth pinning is the
``build.commands`` (generic) path vs the standard doctype path: generic builds
run ``install_build_tools`` + ``run_build_commands`` and must skip
``setup_environment`` + ``build``, while standard builds do the opposite.
Everything else in ``run()`` (API claims, artifact upload, signal handlers) is
stubbed at the runner's own seams.
"""

from pathlib import Path
from unittest import mock

import pytest
from conftest import make_director

from builder.exceptions import BuildAppError
from builder.exceptions import BuildCancelled
from builder.exceptions import BuildUserError
from builder.runner import Runner


def _make_runner(config, *, project=None):
    """Build a Runner whose director is a spy and whose lifecycle is stubbed."""
    director = make_director(config, project=project)
    runner = Runner(director.data)

    # Spy on the director so we can assert which build path was taken.
    runner.director = mock.MagicMock()

    # Stub the lifecycle helpers that would otherwise hit the API, install
    # signal handlers, or touch the filesystem.
    for name in (
        "_install_timeout_handler",
        "_install_cancellation_handler",
        "_reset_build",
        "_claim_build",
        "_post_checkout",
        "_set_build_state",
        "_validate_artifacts",
        "_upload_artifacts",
        "_update_version",
        "_finalize",
    ):
        setattr(runner, name, mock.MagicMock())
    return runner


def test_run_checks_the_old_output_directory(docroot):
    # The legacy ``_build/html`` check must run after the build.
    runner = _make_runner(
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "3"}},
            "sphinx": {"configuration": "conf.py"},
        }
    )

    runner.run()

    runner.director.check_old_output_directory.assert_called_once()


def test_run_refuses_to_build_a_disabled_project(docroot):
    runner = _make_runner(
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "3"}},
            "sphinx": {"configuration": "conf.py"},
        },
        project={"skip": True},
    )

    with pytest.raises(BuildAppError) as excinfo:
        runner.run()

    assert excinfo.value.message_id == BuildAppError.BUILDS_DISABLED
    # Nothing about the build is touched, but it's still finalized so it
    # doesn't stay in flight.
    runner._reset_build.assert_not_called()
    runner._claim_build.assert_not_called()
    runner.director.setup_vcs.assert_not_called()
    runner._finalize.assert_called_once()


def test_on_cancel_raises_when_not_uploading(docroot):
    runner = Runner(make_director().data)
    runner._in_upload = False

    with pytest.raises(BuildCancelled):
        runner._on_cancel(15, None)


def test_on_cancel_deferred_during_upload(docroot):
    # A SIGTERM mid-upload must not interrupt the rclone sync.
    runner = Runner(make_director().data)
    runner._in_upload = True

    # Must not raise.
    assert runner._on_cancel(15, None) is None


def test_run_dispatches_generic_builds_to_build_commands(docroot):
    runner = _make_runner(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
                "commands": ["echo hi"],
            },
        }
    )

    runner.run()

    runner.director.install_build_tools.assert_called_once()
    runner.director.run_build_commands.assert_called_once()
    runner.director.setup_environment.assert_not_called()
    runner.director.build.assert_not_called()


def test_post_checkout_patches_commit_config_and_yaml_path(docroot):
    # ``setup_vcs`` records these; ``_post_checkout`` reports them to the API.
    runner = Runner(make_director().data)
    runner.data.build["commit"] = "abc123"
    runner.data.build["config"] = {"version": 2}
    runner.data.build["readthedocs_yaml_path"] = "docs/.readthedocs.yaml"

    runner._post_checkout()

    runner.data.api_client.build.return_value.patch.assert_called_once_with(
        {
            "commit": "abc123",
            "config": {"version": 2},
            "readthedocs_yaml_path": "docs/.readthedocs.yaml",
        }
    )


def test_post_checkout_patches_config_even_without_a_commit(docroot):
    # An external build may lack a resolved commit, but the config is always set.
    runner = Runner(make_director().data)
    runner.data.build["config"] = {"version": 2}

    runner._post_checkout()

    payload = runner.data.api_client.build.return_value.patch.call_args.args[0]
    assert payload == {"config": {"version": 2}}


def test_post_checkout_noop_when_nothing_resolved(docroot):
    # Nothing recorded on the build yet -> no PATCH.
    runner = Runner(make_director().data)

    runner._post_checkout()

    runner.data.api_client.build.return_value.patch.assert_not_called()


def test_update_version_sends_build_data_when_populated(docroot):
    # ``store_readthedocs_build_yaml`` set ``version.build_data`` from the user's
    # ``readthedocs-build.yaml``; it must reach the version PATCH.
    runner = Runner(make_director().data)
    runner.data.version.build_data = {"tools": {"python": "3.12"}}

    runner._update_version(["html"])

    payload = runner.data.api_client.version.return_value.patch.call_args.args[0]
    assert payload["build_data"] == {"tools": {"python": "3.12"}}


def test_update_version_omits_build_data_when_absent(docroot):
    # No ``readthedocs-build.yaml`` -> build_data stays None and must NOT be sent
    # (sending None would wipe any stored data).
    runner = Runner(make_director().data)
    runner.data.version.build_data = None

    runner._update_version(["html"])

    payload = runner.data.api_client.version.return_value.patch.call_args.args[0]
    assert "build_data" not in payload


def test_run_posts_checkout_metadata_after_setup_vcs(docroot):
    # The post-checkout PATCH happens once, right after the clone.
    runner = _make_runner(
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "3"}},
            "sphinx": {"configuration": "conf.py"},
        }
    )

    runner.run()

    runner._post_checkout.assert_called_once()


def test_run_dispatches_standard_builds_to_build(docroot):
    runner = _make_runner(
        {
            "build": {
                "os": "ubuntu-22.04",
                "tools": {"python": "3"},
            },
            "sphinx": {"configuration": "conf.py"},
        }
    )

    runner.run()

    runner.director.setup_environment.assert_called_once()
    runner.director.build.assert_called_once()
    runner.director.run_build_commands.assert_not_called()


# ---------------------------------------------------------------------------
# Uploaded builds (direct artifact upload)
#
# There is no repository to clone and no config file, so the runner takes a
# shorter path: create the build environment, download the ZIP from the uploads
# bucket, extract it, then hand off to the shared validate/upload tail.
# ---------------------------------------------------------------------------


UPLOADED_BUILD = {
    "id": 1,
    "is_uploaded": True,
    "uploaded_artifacts_storage_path": "uploads/pip/1/artifacts.zip",
}


def _make_uploaded_runner():
    """A stubbed runner for a build with ``is_uploaded=True`` and no config."""
    director = make_director(config=None, build=dict(UPLOADED_BUILD))
    runner = Runner(director.data)
    runner.director = mock.MagicMock()
    for name in (
        "_install_timeout_handler",
        "_install_cancellation_handler",
        "_reset_build",
        "_claim_build",
        "_post_checkout",
        "_set_build_state",
        "_validate_artifacts",
        "_upload_artifacts",
        "_update_version",
        "_finalize",
    ):
        setattr(runner, name, mock.MagicMock())
    return runner


def test_run_downloads_and_extracts_uploaded_artifacts(docroot):
    runner = _make_uploaded_runner()

    runner.run()

    runner.director.download_artifacts_from_storage.assert_called_once()
    runner.director.extract_artifacts.assert_called_once()


def test_run_skips_the_clone_for_uploaded_builds(docroot):
    # No repository is involved: cloning would fail (there may be no repo at
    # all) and there is no config file to parse afterwards.
    runner = _make_uploaded_runner()

    runner.run()

    runner.director.create_vcs_environment.assert_not_called()
    runner.director.setup_vcs.assert_not_called()
    runner._post_checkout.assert_not_called()


def test_run_skips_the_environment_setup_for_uploaded_builds(docroot):
    # The docs are already built; nothing gets installed or run.
    runner = _make_uploaded_runner()

    runner.run()

    runner.director.setup_environment.assert_not_called()
    runner.director.install_build_tools.assert_not_called()
    runner.director.build.assert_not_called()
    runner.director.run_build_commands.assert_not_called()


def test_run_reports_cloning_then_building_for_uploaded_builds(docroot):
    # ``installing`` is skipped — nothing is installed.
    runner = _make_uploaded_runner()

    runner.run()

    states = [call.args[0] for call in runner._set_build_state.call_args_list]
    assert states == ["cloning", "building", "uploading"]


def test_run_validates_and_uploads_uploaded_artifacts(docroot):
    # Uploaded artifacts get the same validation (index.html, single-file
    # formats) and the same storage sync as built ones.
    runner = _make_uploaded_runner()

    assert runner.run() is True
    runner._validate_artifacts.assert_called_once()
    runner._upload_artifacts.assert_called_once()
    runner._update_version.assert_called_once()


def test_run_checks_the_old_output_directory_for_uploaded_builds(docroot):
    runner = _make_uploaded_runner()

    runner.run()

    runner.director.check_old_output_directory.assert_called_once()


def test_run_fails_the_build_when_the_uploaded_zip_is_invalid(docroot):
    runner = _make_uploaded_runner()
    runner.director.extract_artifacts.side_effect = BuildUserError(
        BuildUserError.BUILD_ARTIFACTS_ZIP_INVALID
    )

    assert runner.run() is False
    runner._upload_artifacts.assert_not_called()
    runner._finalize.assert_called_once()
    message_id = runner.director.attach_notification.call_args.kwargs["message_id"]
    assert message_id == BuildUserError.BUILD_ARTIFACTS_ZIP_INVALID


def test_run_treats_a_missing_is_uploaded_flag_as_a_regular_build(docroot):
    # The API omits the key on older payloads; it must not select the upload path.
    runner = _make_runner(
        {
            "build": {"os": "ubuntu-22.04", "tools": {"python": "3"}},
            "sphinx": {"configuration": "conf.py"},
        }
    )
    assert "is_uploaded" not in runner.data.build

    runner.run()

    runner.director.download_artifacts_from_storage.assert_not_called()
    runner.director.setup_vcs.assert_called_once()


# ---------------------------------------------------------------------------
# _validate_artifacts
# ---------------------------------------------------------------------------


def _artifact_dir(runner, artifact_type):
    """Create (and return) the output directory for ``artifact_type``."""
    path = Path(
        runner.data.project.artifact_path(
            version=runner.data.version.slug,
            type_=artifact_type,
        )
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _valid_html(runner):
    (_artifact_dir(runner, "html") / "index.html").write_text("<html></html>")


def test_validate_artifacts_accepts_html_with_an_index(docroot):
    runner = Runner(make_director().data)
    _valid_html(runner)

    # Formats that weren't built are simply absent from the list.
    assert runner._validate_artifacts() == ["html"]


def test_validate_artifacts_requires_an_index_html(docroot):
    runner = Runner(make_director().data)
    _artifact_dir(runner, "html")  # directory exists, but it's empty

    with pytest.raises(BuildUserError) as excinfo:
        runner._validate_artifacts()

    assert excinfo.value.message_id == BuildUserError.BUILD_OUTPUT_HTML_NO_INDEX_FILE


def test_validate_artifacts_renames_single_file_formats(docroot):
    # Proxito serves downloads from ``<project_slug>.<ext>``.
    runner = Runner(make_director().data)
    _valid_html(runner)
    pdf_dir = _artifact_dir(runner, "pdf")
    (pdf_dir / "whatever-name.pdf").write_text("%PDF-1.4")

    assert runner._validate_artifacts() == ["html", "pdf"]
    assert [p.name for p in pdf_dir.iterdir()] == ["pip.pdf"]


def test_validate_artifacts_rejects_multiple_files_in_single_file_formats(docroot):
    runner = Runner(make_director().data)
    _valid_html(runner)
    epub_dir = _artifact_dir(runner, "epub")
    (epub_dir / "one.epub").write_text("")
    (epub_dir / "two.epub").write_text("")

    with pytest.raises(BuildUserError) as excinfo:
        runner._validate_artifacts()

    assert excinfo.value.message_id == BuildUserError.BUILD_OUTPUT_HAS_MULTIPLE_FILES
    assert excinfo.value.format_values == {"artifact_type": "epub"}


def test_validate_artifacts_rejects_empty_single_file_formats(docroot):
    runner = Runner(make_director().data)
    _valid_html(runner)
    _artifact_dir(runner, "htmlzip")

    with pytest.raises(BuildUserError) as excinfo:
        runner._validate_artifacts()

    assert excinfo.value.message_id == BuildUserError.BUILD_OUTPUT_HAS_0_FILES
    assert excinfo.value.format_values == {"artifact_type": "htmlzip"}


def test_validate_artifacts_rejects_an_output_path_that_is_a_file(docroot):
    runner = Runner(make_director().data)
    _valid_html(runner)
    # ``json`` is a plain file instead of the expected directory.
    json_path = Path(
        runner.data.project.artifact_path(version=runner.data.version.slug, type_="json")
    )
    json_path.write_text("{}")

    with pytest.raises(BuildUserError) as excinfo:
        runner._validate_artifacts()

    assert excinfo.value.message_id == BuildUserError.BUILD_OUTPUT_IS_NOT_A_DIRECTORY
    assert excinfo.value.format_values == {"artifact_type": "json"}


# ---------------------------------------------------------------------------
# _upload_artifacts
# ---------------------------------------------------------------------------


def _patch_storage():
    """Patch ``get_storage`` and hand back the storage mock it returns."""
    storage = mock.MagicMock()
    return mock.patch("builder.storage.get_storage", return_value=storage), storage


def test_upload_artifacts_syncs_valid_types_and_deletes_the_rest(docroot):
    runner = Runner(make_director().data)
    patcher, storage = _patch_storage()

    with patcher:
        runner._upload_artifacts(["html", "pdf"])

    synced = {call.args[1] for call in storage.rclone_sync_directory.call_args_list}
    assert synced == {"html/pip/latest", "pdf/pip/latest"}
    # The source is the local artifact directory.
    from_paths = [call.args[0] for call in storage.rclone_sync_directory.call_args_list]
    assert from_paths[0] == runner.data.project.artifact_path(version="latest", type_="html")

    deleted = {call.args[0] for call in storage.delete_directory.call_args_list}
    # ``json`` is undeletable, so it survives even though it wasn't built.
    assert deleted == {"htmlzip/pip/latest", "epub/pip/latest"}


def test_upload_artifacts_never_deletes_html_and_json(docroot):
    # A build that produced nothing must not wipe previously published docs.
    runner = Runner(make_director().data)
    patcher, storage = _patch_storage()

    with patcher:
        runner._upload_artifacts([])

    storage.rclone_sync_directory.assert_not_called()
    deleted = {call.args[0] for call in storage.delete_directory.call_args_list}
    assert deleted == {"htmlzip/pip/latest", "pdf/pip/latest", "epub/pip/latest"}


def test_upload_artifacts_wraps_sync_errors_in_a_build_app_error(docroot):
    runner = Runner(make_director().data)
    patcher, storage = _patch_storage()
    storage.rclone_sync_directory.side_effect = OSError("boom")

    with patcher, pytest.raises(BuildAppError) as excinfo:
        runner._upload_artifacts(["html"])

    assert excinfo.value.message_id == BuildAppError.UPLOAD_FAILED


def test_upload_artifacts_reraises_build_exceptions_untouched(docroot):
    # A cancellation mid-upload must stay a cancellation, not become an app error.
    runner = Runner(make_director().data)
    patcher, storage = _patch_storage()
    storage.rclone_sync_directory.side_effect = BuildCancelled(BuildCancelled.CANCELLED_BY_USER)

    with patcher, pytest.raises(BuildCancelled):
        runner._upload_artifacts(["html"])


def test_upload_artifacts_survives_delete_failures(docroot):
    # Deleting a prefix that doesn't exist must not fail the build.
    runner = Runner(make_director().data)
    patcher, storage = _patch_storage()
    storage.delete_directory.side_effect = OSError("nope")

    with patcher:
        runner._upload_artifacts(["html"])

    storage.rclone_sync_directory.assert_called_once()


def test_validate_artifacts_leaves_an_already_named_file_alone(docroot):
    # The file is already ``<project_slug>.<ext>``; no move needed.
    runner = Runner(make_director().data)
    _valid_html(runner)
    pdf_dir = _artifact_dir(runner, "pdf")
    (pdf_dir / "pip.pdf").write_text("%PDF-1.4")

    assert runner._validate_artifacts() == ["html", "pdf"]
    assert (pdf_dir / "pip.pdf").read_text() == "%PDF-1.4"

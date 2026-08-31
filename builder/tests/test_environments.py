"""
Tests for the build environment and command runner.

Ported from ``readthedocs/rtd_tests/tests/test_doc_building.py``. Differences
from upstream, all intentional:

- The ``DockerBuildEnvironment`` / ``DockerBuildCommand`` suites are dropped:
  the runner executes plainly inside the build container, so there's no Docker
  layer. Upstream had already skipped/deferred those tests.
- ``LocalBuildEnvironment`` is now just ``BuildEnvironment``.
- ``DockerBuildCommand`` is covered with a mocked Docker client rather than a
  real daemon: the wrapping logic is what's worth asserting on, and the suite
  must stay runnable without Docker.
- ``BuildEnvironment`` (the local subprocess path) keeps its coverage because
  the VCS and LaTeX suites depend on it — see the note on that class.
"""

from unittest import mock

import pytest
from docker.errors import APIError as DockerAPIError
from slumber.exceptions import HttpNotFoundError

from builder.api_models import APIProject
from builder.constants import RTD_SKIP_BUILD_EXIT_CODE
from builder.environments import BuildCommand
from builder.environments import BuildEnvironment
from builder.environments import DockerBuildCommand
from builder.environments import DockerBuildEnvironment
from builder.environments import _expand_env_vars
from builder.environments import _killed_by_oom
from builder.environments import _truncate_output
from builder.exceptions import BuildAppError
from builder.exceptions import BuildCancelled
from builder.exceptions import BuildUserError


SAMPLE_UNICODE = "HérÉ îß sömê ünïçó∂é"
SAMPLE_UTF8_BYTES = SAMPLE_UNICODE.encode("utf-8")


def make_env(tmp_path=None, **kwargs):
    """A non-recording BuildEnvironment with a project and build attached."""
    kwargs.setdefault("project", APIProject(slug="test-project"))
    kwargs.setdefault("build", {"id": 1})
    kwargs.setdefault("record", False)
    return BuildEnvironment(**kwargs)


def make_docker_env(**kwargs):
    """A non-recording DockerBuildEnvironment pointed at a fake container."""
    kwargs.setdefault("project", APIProject(slug="test-project"))
    kwargs.setdefault("build", {"id": 1})
    kwargs.setdefault("record", False)
    kwargs.setdefault("container_name", "build-1")
    return DockerBuildEnvironment(**kwargs)


# ---------------------------------------------------------------------------
# _expand_env_vars
# ---------------------------------------------------------------------------


def test_expand_env_vars_expands_dollar_name():
    assert _expand_env_vars("$FOO/bar", {"FOO": "abc"}) == "abc/bar"


def test_expand_env_vars_expands_braced_name():
    assert _expand_env_vars("${FOO}bar", {"FOO": "abc"}) == "abcbar"


def test_expand_env_vars_leaves_unknown_vars_untouched():
    assert _expand_env_vars("$UNKNOWN/x", {}) == "$UNKNOWN/x"


@pytest.mark.parametrize("value", ["$$", "$(command)", "$1positional"])
def test_expand_env_vars_ignores_non_variable_dollars(value):
    # Only ``$NAME`` / ``${NAME}`` are expanded; bare ``$$``, ``$(...)`` and
    # ``$1`` (no leading letter/underscore) pass through untouched.
    assert _expand_env_vars(value, {}) == value


def test_expand_env_vars_passes_through_non_strings():
    assert _expand_env_vars(123, {}) == 123


# ---------------------------------------------------------------------------
# DockerBuildCommand command wrapping
# ---------------------------------------------------------------------------


def make_docker_command(command, **kwargs):
    kwargs.setdefault("build_env", make_docker_env())
    return DockerBuildCommand(command, **kwargs)


def test_wrapped_command_runs_through_a_shell_under_nice():
    cmd = make_docker_command(("echo", "hi"))
    assert cmd.get_wrapped_command() == "nice -n 10 /bin/sh -c 'echo hi'"


def test_wrapped_command_prepends_bin_path_to_the_container_path():
    cmd = make_docker_command(("python", "-V"), bin_path="/venv/bin")
    assert cmd.get_wrapped_command() == "nice -n 10 /bin/sh -c 'PATH=/venv/bin:$PATH ; python -V'"


def test_wrapped_command_escapes_shell_metacharacters():
    cmd = make_docker_command(("pip", "install", "requests<0.8"))
    assert cmd.get_wrapped_command() == "nice -n 10 /bin/sh -c 'pip install requests\\<0.8'"


def test_wrapped_command_leaves_user_commands_unescaped():
    cmd = make_docker_command(("cat foo.txt | grep bar",), escape_command=False)
    assert cmd.get_wrapped_command() == "nice -n 10 /bin/sh -c 'cat foo.txt | grep bar'"


@pytest.mark.parametrize(
    "variable",
    [
        "READTHEDOCS_OUTPUT",
        "READTHEDOCS_REPOSITORY_PATH",
        "READTHEDOCS_VIRTUALENV_PATH",
        "READTHEDOCS_GIT_CLONE_TOKEN",
        "CONDA_ENVS_PATH",
        "CONDA_DEFAULT_ENV",
    ],
)
def test_wrapped_command_keeps_allowlisted_variables_expandable(variable):
    """These must reach the container's shell unescaped so it expands them."""
    cmd = make_docker_command(("cp", "-r", "html", f"${variable}"))
    assert f"${variable}" in cmd.get_wrapped_command()
    assert f"\\${variable}" not in cmd.get_wrapped_command()


def test_command_without_a_container_fails_loudly():
    cmd = make_docker_command(("echo", "hi"), build_env=make_docker_env(container_name=""))
    with pytest.raises(BuildAppError):
        cmd.run()


def test_docker_command_execs_into_the_container():
    build_env = make_docker_env()
    client = mock.Mock()
    client.exec_create.return_value = {"Id": "exec-id"}
    client.exec_start.return_value = b"output"
    client.exec_inspect.return_value = {"ExitCode": 0}
    build_env.client = client

    cmd = DockerBuildCommand(("echo", "hi"), build_env=build_env, user="docs", cwd="/tmp")
    cmd.run()

    _, kwargs = client.exec_create.call_args
    assert kwargs["container"] == "build-1"
    assert kwargs["user"] == "docs"
    assert kwargs["workdir"] == "/tmp"
    assert kwargs["cmd"] == "nice -n 10 /bin/sh -c 'echo hi'"
    assert cmd.output == "output"
    assert cmd.exit_code == 0


def test_docker_command_records_an_api_error_as_a_failure():
    build_env = make_docker_env()
    client = mock.Mock()
    client.exec_create.side_effect = DockerAPIError("boom")
    build_env.client = client

    cmd = DockerBuildCommand(("echo", "hi"), build_env=build_env)
    cmd.run()

    assert cmd.exit_code == -1
    assert cmd.output == "Command exited abnormally"


# ---------------------------------------------------------------------------
# _truncate_output
# ---------------------------------------------------------------------------


def test_truncate_output_leaves_short_output_untouched():
    output = "\n".join(str(i) for i in range(5))
    assert _truncate_output(output) == output


def test_truncate_output_elides_long_output():
    output = "\n".join(str(i) for i in range(50))
    truncated = _truncate_output(output)
    assert " ..Output Truncated.. " in truncated
    assert truncated.startswith("0\n1")
    assert truncated.endswith("48\n49")


def test_truncate_output_handles_none():
    assert _truncate_output(None) == ""


# ---------------------------------------------------------------------------
# BuildCommand construction & result
# ---------------------------------------------------------------------------


def test_command_stores_environment():
    env = {"FOOBAR": "foobar", "BIN_PATH": "foobar"}
    cmd = BuildCommand(["echo"], environment=env)
    assert cmd._environment["FOOBAR"] == "foobar"
    assert cmd._environment["BIN_PATH"] == "foobar"


def test_command_rejects_a_path_in_the_environment():
    with pytest.raises(BuildAppError) as excinfo:
        BuildCommand(["echo"], environment={"PATH": "/usr/bin"})
    assert excinfo.value.message_id == BuildAppError.GENERIC_WITH_BUILD_ID


def test_command_rejects_a_bare_string_in_exec_mode():
    # A string command in exec mode would char-split, so it's rejected outright.
    with pytest.raises(TypeError):
        BuildCommand("echo hi")


def test_command_rejects_a_bare_string():
    # Callers go through ``BuildEnvironment.run(*cmd)``, which always produces
    # a tuple. A bare string would char-split when the parts are joined.
    with pytest.raises(TypeError):
        BuildCommand("echo hi", escape_command=False)


def test_command_true_is_successful():
    cmd = BuildCommand(["true"], cwd="/tmp")
    cmd.run()
    assert cmd.successful is True
    assert cmd.failed is False
    assert cmd.finished is True


def test_command_false_fails():
    cmd = BuildCommand(["false"], cwd="/tmp")
    cmd.run()
    assert cmd.failed is True


def test_command_result_mixin_finished_before_run():
    cmd = BuildCommand(["true"], cwd="/tmp")
    assert cmd.finished is False


def test_missing_command_reports_exit_code_minus_one():
    cmd = BuildCommand(["/non-existent/binary-xyz"], cwd="/tmp")
    cmd.run()
    assert cmd.exit_code == -1
    # No output is captured because the process never started.
    assert cmd.output is None
    assert cmd.error is None


def test_command_captures_stdout():
    cmd = BuildCommand(["/bin/bash", "-c", "echo -n FOOBAR"], cwd="/tmp")
    cmd.run()
    assert cmd.output == "FOOBAR"


def test_command_combines_stderr_into_stdout_by_default():
    cmd = BuildCommand(["/bin/bash", "-c", "echo -n FOOBAR 1>&2"], cwd="/tmp")
    cmd.run()
    # Without demux, stderr is folded into stdout and error stays empty.
    assert cmd.output == "FOOBAR"
    assert cmd.error == ""


def test_command_demux_separates_stderr():
    cmd = BuildCommand(["/bin/bash", "-c", "echo -n OUT; echo -n ERR 1>&2"], cwd="/tmp", demux=True)
    cmd.run()
    assert cmd.output == "OUT"
    assert cmd.error == "ERR"


def test_command_expands_env_vars_in_exec_mode():
    cmd = BuildCommand(
        ["/bin/bash", "-c", "echo -n $FOO"],
        cwd="/tmp",
        environment={"FOO": "expanded"},
    )
    cmd.run()
    assert cmd.output == "expanded"


def test_command_shell_mode_runs_a_shell_expression():
    cmd = BuildCommand(("echo -n FROM_SHELL",), cwd="/tmp", escape_command=False)
    cmd.run()
    assert cmd.output == "FROM_SHELL"


def test_command_does_not_override_home(monkeypatch):
    # HOME is no longer derived from a passwd lookup: on the Docker path the
    # container's ``--user`` resolves it, and looking it up on the host would
    # be answering a question about the wrong machine.
    monkeypatch.setenv("HOME", "/runtime/home")
    cmd = BuildCommand(["/bin/sh", "-c", "echo -n $HOME"], cwd="/tmp", user="root")
    cmd.run()
    assert cmd.output == "/runtime/home"


def test_command_unicode_output():
    cmd = BuildCommand(["printf", SAMPLE_UNICODE], cwd="/tmp")
    cmd.run()
    assert cmd.output == SAMPLE_UNICODE


def test_command_decodes_invalid_bytes_with_replacement():
    # A lone continuation byte isn't valid UTF-8 and is replaced, not raised.
    assert BuildCommand(["true"]).decode_output(b"\xff") == "�"


def test_command_decode_output_handles_non_bytes():
    assert BuildCommand(["true"]).decode_output(None) == ""


def test_str_includes_command_and_output():
    cmd = BuildCommand(["/bin/bash", "-c", "echo -n hi"], cwd="/tmp")
    cmd.run()
    text = str(cmd)
    assert "/bin/bash -c echo -n hi" in text


def test_get_command_flattens_a_list():
    assert BuildCommand(["git", "clone", "url"]).get_command() == "git clone url"


def test_get_command_joins_the_parts():
    assert BuildCommand(("git", "clone", "url")).get_command() == "git clone url"


# ---------------------------------------------------------------------------
# sanitize_output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, sanitized",
    [
        ("Hola", "Hola"),
        ("H\x00i", "Hi"),
        ("H\x00i \x00\x00\x00You!\x00", "Hi You!"),
    ],
)
def test_sanitize_output_strips_null_bytes(output, sanitized):
    cmd = BuildCommand(["/bin/bash", "-c", "echo"])
    assert cmd.sanitize_output(output) == sanitized


def test_sanitize_output_truncates_oversized_output():
    cmd = BuildCommand(["/bin/bash", "-c", "echo"])
    big = "x" * 3_000_000
    sanitized = cmd.sanitize_output(big)
    assert sanitized.startswith(".. (truncated) ...")
    assert len(sanitized.encode("utf-8")) < len(big.encode("utf-8"))


def test_sanitize_output_obfuscates_private_env_vars():
    project = APIProject(
        slug="test-project",
        environment_variables={
            "PUBLIC": {"public": True, "value": "public-value"},
            "PRIVATE": {"public": False, "value": "private-value"},
        },
    )
    build_env = make_env(project=project)
    cmd = BuildCommand(["/bin/bash", "-c", "echo"], build_env=build_env)
    # Public values are left alone; private ones keep only their first 4 chars.
    assert cmd.sanitize_output("public-value") == "public-value"
    assert cmd.sanitize_output("private-value") == "priv****"


# ---------------------------------------------------------------------------
# BuildCommand.save
# ---------------------------------------------------------------------------


def test_save_posts_a_new_command_then_patches():
    api_client = mock.MagicMock()
    api_client.command.post.return_value = {"id": 42}
    build_env = make_env()
    cmd = BuildCommand(["/bin/bash", "-c", "echo -n hi"], build_env=build_env, cwd="/tmp")
    cmd.run()

    cmd.save(api_client=api_client)
    assert cmd.id == 42
    api_client.command.post.assert_called_once()

    # A second save PATCHes the existing record rather than POSTing again.
    api_client.command(42).patch.return_value = {"id": 42}
    cmd.save(api_client=api_client)
    api_client.command(42).patch.assert_called_once()


def test_save_falls_back_to_post_when_patch_404s():
    api_client = mock.MagicMock()
    api_client.command.post.return_value = {"id": 7}
    api_client.command(7).patch.side_effect = HttpNotFoundError("gone")
    build_env = make_env()
    cmd = BuildCommand(["/bin/bash", "-c", "echo -n hi"], build_env=build_env, cwd="/tmp")
    cmd.run()
    cmd.save(api_client=api_client)  # POST, remembers id 7

    cmd.save(api_client=api_client)  # PATCH 404s -> re-POST
    assert api_client.command.post.call_count == 2


def test_save_forces_exit_code_zero_when_record_as_success():
    api_client = mock.MagicMock()
    api_client.command.post.return_value = {"id": 1}
    build_env = make_env()
    cmd = BuildCommand(["false"], build_env=build_env, cwd="/tmp", record_as_success=True)
    cmd.run()
    assert cmd.failed is True

    cmd.save(api_client=api_client)
    posted = api_client.command.post.call_args[0][0]
    assert posted["exit_code"] == 0


# ---------------------------------------------------------------------------
# BuildEnvironment.run / run_command_class
# ---------------------------------------------------------------------------


def test_environment_requires_api_client_when_recording():
    with pytest.raises(ValueError):
        BuildEnvironment(record=True)


def test_run_with_record_false_is_not_recorded():
    api_client = mock.MagicMock()
    build_env = BuildEnvironment(api_client=api_client, record=True)
    build_env.run("true", cwd="/tmp", record=False)
    assert len(build_env.commands) == 0
    api_client.command.post.assert_not_called()


def test_run_records_a_command():
    api_client = mock.MagicMock()
    api_client.command.post.return_value = {"id": 1}
    build_env = make_env(api_client=api_client, record=True)
    build_env.run("true", cwd="/tmp")
    assert len(build_env.commands) == 1
    assert build_env.commands[0].successful is True


def test_run_record_as_success_records_a_failure_as_zero():
    api_client = mock.MagicMock()
    api_client.command.post.return_value = {"id": 1}
    build_env = make_env(api_client=api_client, record=True)
    cmd = build_env.run("false", cwd="/tmp", record_as_success=True)
    # record_as_success forces recording on and swallows the failure.
    assert cmd.exit_code == 0
    assert len(build_env.commands) == 1


def test_run_rejects_environment_kwarg():
    build_env = make_env()
    with pytest.raises(BuildAppError) as excinfo:
        build_env.run("true", cwd="/tmp", environment={"FOO": "bar"})
    assert excinfo.value.message_id == BuildAppError.GENERIC_WITH_BUILD_ID


def test_run_maps_bin_path_from_environment():
    build_env = make_env(environment={"BIN_PATH": "/custom/bin"})
    cmd = build_env.run("true", cwd="/tmp")
    assert cmd.bin_path == "/custom/bin"


def test_run_raises_build_user_error_on_failure():
    build_env = make_env()
    with pytest.raises(BuildUserError) as excinfo:
        build_env.run("false", cwd="/tmp")
    assert excinfo.value.message_id == BuildUserError.GENERIC


def test_run_with_record_false_warns_instead_of_raising():
    build_env = make_env()
    # record=False implies warn_only, so a failure is swallowed.
    cmd = build_env.run("false", cwd="/tmp", record=False)
    assert cmd.failed is True


def test_run_skip_exit_code_raises_build_cancelled():
    build_env = make_env()
    with pytest.raises(BuildCancelled) as excinfo:
        build_env.run(
            "/bin/bash", "-c", f"exit {RTD_SKIP_BUILD_EXIT_CODE}",
            cwd="/tmp",
        )
    assert excinfo.value.message_id == BuildCancelled.SKIPPED_EXIT_CODE_183


# ---------------------------------------------------------------------------
# OOM / excessive-memory detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exit_code, output, expected",
    [
        (137, "", True),  # shell reports 128+SIGKILL
        (-9, "", True),  # subprocess reports a SIGKILL as -9
        (1, "a\nKilled\nb", True),  # kernel "Killed" line, exit 1
        (1, "just a normal failure", False),
        (0, "Killed", False),  # success is never an OOM
        (2, "Killed", False),  # non-1 codes don't trust the output text
    ],
)
def test_killed_by_oom(exit_code, output, expected):
    assert _killed_by_oom(exit_code, output) is expected


def test_command_oom_kill_annotates_output():
    cmd = BuildCommand(["/bin/bash", "-c", "kill -9 $$"], cwd="/tmp")
    cmd.run()
    assert cmd.exit_code == -9
    assert "excessive memory" in cmd.output


def test_run_raises_excessive_memory_on_oom_kill():
    build_env = make_env()
    with pytest.raises(BuildUserError) as excinfo:
        build_env.run("/bin/bash", "-c", "kill -9 $$", cwd="/tmp")
    assert excinfo.value.message_id == BuildUserError.BUILD_EXCESSIVE_MEMORY


# ---------------------------------------------------------------------------
# record / warn_only coupling
# ---------------------------------------------------------------------------


def test_unrecorded_commands_only_warn_by_default(tmp_path):
    """Nowhere to report a failure, so don't fail the build over it."""
    env = make_env()

    cmd = env.run("false", record=False)

    assert cmd.exit_code != 0


def test_an_unrecorded_command_can_still_be_made_fatal(tmp_path):
    """
    ``mkdir`` of the checkout dir uses this.

    Swallowing it leaves the next command to die on a cwd that was never
    created, which is a much harder failure to read.
    """
    env = make_env()

    with pytest.raises(BuildUserError):
        env.run("false", record=False, warn_only=False)


def test_every_variable_our_commands_use_survives_escaping():
    """
    The allowlist must cover every ``$VAR`` our own commands reference.

    A variable that isn't listed is escaped to ``\\$NAME``, so the container's
    shell passes it through as a literal string — which surfaces far away from
    the cause, as a command complaining about a path that starts with a dollar
    sign. That is exactly how ``$READTHEDOCS_REPOSITORY_PATH`` broke uploaded
    builds: expansion used to happen in Python, and moving to ``docker exec``
    made it the shell's job.
    """
    import re
    from pathlib import Path

    source = Path(__file__).parent.parent / "builder"
    referenced = set()
    for path in list(source.glob("*.py")) + list(source.glob("backends/*.py")):
        for name in re.findall(r"\$([A-Z_][A-Z0-9_]*)", path.read_text()):
            referenced.add(name)

    # ``PATH`` is prepended by ``get_wrapped_command`` itself, not escaped
    # here; NAME/VAR are placeholders in docstrings.
    referenced -= {"PATH", "NAME", "VAR"}

    for variable in referenced:
        command = DockerBuildCommand((f"${variable}",), build_env=make_docker_env())
        assert f"${variable}" in command.get_wrapped_command()
        assert f"\\${variable}" not in command.get_wrapped_command(), (
            f"${variable} is used in a command but missing from "
            "DockerBuildCommand._escape_command's allowlist"
        )

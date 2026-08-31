"""Unit tests for the shared SSH helpers."""

from builder.ssh import GIT_SSH_COMMAND
from builder.ssh import parse_ssh_agent_env


def test_git_ssh_command_disables_host_key_prompts():
    assert "StrictHostKeyChecking=no" in GIT_SSH_COMMAND
    assert "UserKnownHostsFile=/dev/null" in GIT_SSH_COMMAND


def test_parse_ssh_agent_env_extracts_sock_and_pid():
    output = (
        "SSH_AUTH_SOCK=/tmp/ssh-abc/agent.123; export SSH_AUTH_SOCK;\n"
        "SSH_AGENT_PID=124; export SSH_AGENT_PID;\n"
        "echo Agent pid 124;\n"
    )
    env = parse_ssh_agent_env(output)
    assert env == {
        "SSH_AUTH_SOCK": "/tmp/ssh-abc/agent.123",
        "SSH_AGENT_PID": "124",
    }


def test_parse_ssh_agent_env_ignores_non_export_lines():
    assert parse_ssh_agent_env("Agent pid 124\n\nnot a var line\n") == {}


def test_parse_ssh_agent_env_handles_empty_output():
    assert parse_ssh_agent_env("") == {}

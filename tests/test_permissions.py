"""Module 3 foundation: real gate/file actions, not a second permission runner."""
import json

import pytest

from nexus.permissions import Permissions
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, build_tools


def policy(repo, mode, tools=None):
    import yaml
    (repo / 'permissions.yaml').write_text(yaml.safe_dump({'schema': 1, 'default': mode, 'tools': tools or {}}))


def registry(repo, *, callback=None, mode='auto', outside=False):
    gate = ApprovalGate(mode=mode, callback=callback)
    gate.permissions = Permissions(repo)
    return build_tools(ToolContext(repo, PathGuard(repo, allow_outside=outside), gate))


def test_missing_policy_defaults_to_ask_even_with_yes_and_saved_scope(repo):
    (repo / 'permissions.yaml').unlink()
    reg = registry(repo)
    reg.ctx.gate.always_allow.add('write_file')
    output, error = reg.call('write_file', {'path': 'new.txt', 'content': 'not authorized'})
    assert error and not (repo / 'new.txt').exists() and 'fresh human' in output


def test_ask_every_time_ignores_session_approval_and_reloads_operator_changes(repo):
    policy(repo, 'ASK_EVERY_TIME')
    calls = []
    reg = registry(repo, callback=lambda *a: calls.append(a) or ApprovalDecision(True, scope='always'))
    for content in ('first', 'second'):
        assert not reg.call('write_file', {'path': 'new.txt', 'content': content})[1]
    assert len(calls) == 2
    policy(repo, 'READ_ONLY')
    assert reg.call('write_file', {'path': 'new.txt', 'content': 'third'})[1]
    assert (repo / 'new.txt').read_text() == 'second'


def test_auto_allow_cannot_override_a_caller_denial_or_plan_mode(repo):
    policy(repo, 'AUTO_ALLOW')
    assert registry(repo, mode='ask', callback=lambda *a: ApprovalDecision(False)).call('write_file', {'path': 'x', 'content': 'no'})[1]
    assert registry(repo, mode='plan').call('write_file', {'path': 'x', 'content': 'no'})[1]
    assert not (repo / 'x').exists()
    assert not registry(repo).call('write_file', {'path': 'x', 'content': 'yes'})[1]


def test_per_tool_ceiling_and_existing_native_policy_are_preserved(repo):
    policy(repo, 'AUTO_ALLOW', {'write_file': 'READ_ONLY'})
    reg = registry(repo)
    assert reg.call('write_file', {'path': 'x', 'content': 'no'})[1]
    assert not reg.call('read_file', {'path': 'README.md'})[1]
    policy(repo, 'AUTO_ALLOW')
    reg.ctx.gate.policy = lambda *a: 'Existing Studio role/tool ceiling refuses'
    assert reg.call('write_file', {'path': 'x', 'content': 'no'})[1]
    reg.ctx.gate.policy = None
    reg.ctx.gate.never_allow.add('write_file')
    assert reg.call('write_file', {'path': 'x', 'content': 'no'})[1]


@pytest.mark.parametrize('args', [{'path': '../outside'}, {'path': '/etc/passwd'}, {'path': '.ENV'}, {'path': '.git/config'}])
def test_policy_rejects_outside_and_private_paths_even_with_legacy_allow_outside(repo, args):
    policy(repo, 'AUTO_ALLOW')
    output, error = registry(repo, outside=True).call('read_file', args)
    assert error and 'validation failed' in output


def test_policy_is_operator_owned_and_symlink_escape_is_denied(repo, tmp_path):
    policy(repo, 'AUTO_ALLOW')
    reg = registry(repo)
    assert reg.call('write_file', {'path': 'permissions.yaml', 'content': 'default: AUTO_ALLOW'})[1]
    target = tmp_path / 'outside'
    target.write_text('private')
    (repo / 'link.txt').symlink_to(target)
    assert reg.call('read_file', {'path': 'link.txt'})[1]


@pytest.mark.parametrize('text', ['[]', 'schema: 2', 'default: yes', 'default: []', 'tools: {write_file: [AUTO_ALLOW]}', 'tools: {"*": AUTO_ALLOW}', 'default: [invalid'])
def test_invalid_configuration_fails_closed_without_echoing_yaml(repo, text):
    (repo / 'permissions.yaml').write_text(text)
    output, error = registry(repo).call('write_file', {'path': 'x', 'content': 'no'})
    assert error and 'validation failed' in output and not (repo / 'x').exists()


def test_shell_and_destructive_calls_still_need_fresh_approval_under_auto_allow(repo):
    policy(repo, 'AUTO_ALLOW')
    reg = registry(repo)
    assert reg.call('run_command', {'command': 'echo not-authorized'})[1]
    assert reg.call('delete_file', {'path': 'README.md'})[1]
    assert (repo / 'README.md').exists()


@pytest.mark.parametrize('args', [['config', '--global', 'user.name', 'mutation'], ['branch', '-D', 'other'], ['tag', 'new-tag']])
def test_read_only_cannot_be_bypassed_by_git_subcommand_labels(repo, args):
    policy(repo, 'READ_ONLY')
    assert registry(repo).call('git', {'args': args})[1]


def test_audit_is_digest_only_durable_and_does_not_write_project_files(repo):
    from nexus.config import nexus_home
    policy(repo, 'READ_ONLY')
    reg = registry(repo)
    secret = 'ghp_' + 'x' * 36
    reg.call('write_file', {'path': 'x', 'content': secret})
    assert not reg.call('read_file', {'path': 'README.md'})[1]
    logs = list((nexus_home() / 'permissions').glob('*/audit.jsonl'))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert secret not in text and 'content' not in text and not (repo / '.nexus').exists()
    event = json.loads(text.splitlines()[-1])
    assert event['tool'] == 'read_file' and event['approved'] and len(event['args_sha256']) == 64


def test_nexus_agent_really_consumes_permissions_yaml(repo):
    from nexus.agent import NexusAgent
    policy(repo, 'READ_ONLY')
    agent = NexusAgent(workspace=repo, auto_yes=True)
    _, reg, _ = agent.tools_for(agent.config.role('general'))
    assert reg.call('write_file', {'path': 'x', 'content': 'no'})[1]
    assert not (repo / 'x').exists()


def test_revocation_while_approval_waits_blocks_execution(repo):
    policy(repo, 'ASK_EVERY_TIME')
    def approve(*args):
        policy(repo, 'READ_ONLY')
        return ApprovalDecision(True)
    reg = registry(repo, callback=approve)
    output, error = reg.call('write_file', {'path': 'x', 'content': 'not permitted after revocation'})
    assert error and 'changed during approval' in output and not (repo / 'x').exists()


def test_cancel_during_approval_blocks_file_write_not_just_subprocess(repo):
    import threading
    policy(repo, 'ASK_EVERY_TIME')
    cancel = threading.Event()
    def approve(*args):
        cancel.set()
        return ApprovalDecision(True)
    reg = registry(repo, callback=approve)
    reg.ctx.cancel_event = cancel
    output, error = reg.call('write_file', {'path': 'x', 'content': 'no'})
    assert error and 'cancelled' in output and not (repo / 'x').exists()


def test_hardlink_alias_cannot_change_operator_policy(repo):
    import os
    policy(repo, 'AUTO_ALLOW')
    os.link(repo / 'permissions.yaml', repo / 'innocent.txt')
    output, error = registry(repo).call('write_file', {'path': 'innocent.txt', 'content': 'default: AUTO_ALLOW'})
    assert error and 'validation failed' in output
    assert 'schema:' in (repo / 'permissions.yaml').read_text()


def test_plugin_code_is_operator_owned_in_auto_allow_mode(repo):
    policy(repo, 'AUTO_ALLOW')
    reg = registry(repo)
    for filename in ('plugins/new.py', 'plugins/words.mcp.json'):
        output, error = reg.call('write_file', {'path': filename, 'content': 'not operator code'})
        assert error and 'no tool execution' in output
        assert not (repo / filename).exists()


def test_custom_plugin_directory_cannot_be_edited_or_restored(repo):
    import yaml

    from nexus.snapshots import Snapshots
    (repo / 'mcp.yaml').write_text(yaml.safe_dump({'enabled': False, 'plugin_directory': 'trusted-tools'}))
    policy(repo, 'AUTO_ALLOW')
    reg = registry(repo)
    output, error = reg.call('write_file', {'path': 'trusted-tools/code.py', 'content': 'not operator code'})
    assert error and 'no tool execution' in output
    with pytest.raises(ValueError):
        Snapshots(repo).target('trusted-tools/code.py')

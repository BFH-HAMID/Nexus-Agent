"""Semantic drafts are not authority; pinned macros reuse the live scoped registry."""
from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from nexus.computer_control import EmergencyStop
from nexus.computer_workflows import Recorder, voice_request
from nexus.tools import ApprovalDecision

from .computer_helpers import FakeDesktop, desktop, home, macro, policy, registry


def steps():
    return [{'tool': 'write_file', 'args': {'path': 'first.txt', 'content': 'one'}},
            {'tool': 'write_file', 'args': {'path': 'second.txt', 'content': '${body}'}}]


def test_named_parameterized_replay_reviews_each_step_in_same_registry(repo):
    macro(repo, 'deploy', steps(), parameters={'body': {'type': 'string', 'max_length': 100}})
    reg, calls = registry(repo)
    output, error = reg.call('workflow_run', {'name': 'deploy', 'parameters': {'body': 'two'}})
    assert not error, output
    assert (repo / 'second.txt').read_text() == 'two'
    assert [row[0] for row in calls] == ['workflow_run', 'write_file', 'write_file']
    assert json.loads(output)['reused_approvals'] == 0


def test_macro_cannot_widen_role_pruned_registry(repo):
    macro(repo, 'routine', steps(), parameters={'body': {'type': 'string'}})
    reg, calls = registry(repo)
    reg.keep_only({'workflow_run'})
    assert reg.call('workflow_run', {'name': 'routine', 'parameters': {'body': 'x'}})[1]
    assert len(calls) == 1 and not (repo / 'first.txt').exists()


@pytest.mark.parametrize('when', ['before', 'approval'])
def test_changed_macro_needs_operator_repin(repo, when):
    _cfg, path = macro(repo, 'routine', [{'tool': 'write_file', 'args': {'path': 'new.txt', 'content': 'a'}}])
    reg, calls = registry(repo)
    if when == 'before':
        path.write_text(path.read_text() + '\n# changed\n')
    else:
        def approve(*_):
            path.write_text(path.read_text() + '\n# changed\n')
            return ApprovalDecision(True)
        reg.ctx.gate.callback = approve
    assert reg.call('workflow_run', {'name': 'routine'})[1]
    assert not (repo / 'new.txt').exists()


@pytest.mark.parametrize('kind', ['nested', 'shell-template', 'partial-template', 'unknown-parameter', 'extra-parameter'])
def test_macros_are_data_not_evaluable_instructions(repo, kind):
    action = {'tool': 'write_file', 'args': {'path': '${path}', 'content': 'literal'}}
    params, values = {'path': {'type': 'string'}}, {'path': 'safe.txt'}
    if kind == 'nested':
        action = {'tool': 'workflow_run', 'args': {'name': 'other'}}
    elif kind == 'shell-template':
        action = {'tool': 'run_command', 'args': {'command': '${path}'}}
    elif kind == 'partial-template':
        action['args']['path'] = 'some/${path}.txt'
    elif kind == 'unknown-parameter':
        action['args']['path'] = '${other}'
    else:
        values['unused'] = 'new permission'
    allowed = ['write_file', 'run_command']
    macro(repo, 'routine', [action], parameters=params, allowed=allowed)
    reg, calls = registry(repo)
    assert reg.call('workflow_run', {'name': 'routine', 'parameters': values})[1]
    assert not calls and not (repo / 'safe.txt').exists()


def test_macro_denial_stops_and_does_not_rollback_or_reuse_a_grant(repo):
    macro(repo, 'routine', steps(), parameters={'body': {'type': 'string'}})
    def approve(spec, args, *_):
        return ApprovalDecision(spec.name != 'write_file' or args['path'] == 'first.txt', scope='session')
    reg, calls = registry(repo, callback=approve)
    output, error = reg.call('workflow_run', {'name': 'routine', 'parameters': {'body': 'x'}})
    assert error and 'step 2' in output and 'not rolled back' in output
    assert (repo / 'first.txt').exists() and not (repo / 'second.txt').exists()
    assert len(calls) == 3


def test_macro_operator_revocation_between_steps_stops_next_step(repo):
    macro(repo, 'routine', steps(), parameters={'body': {'type': 'string'}})
    reg, _calls = registry(repo)
    write = reg._tools['write_file'].handler
    def changed(args):
        result = write(args)
        EmergencyStop().set(True)
        return result
    reg._tools['write_file'].handler = changed
    try:
        assert reg.call('workflow_run', {'name': 'routine', 'parameters': {'body': 'x'}})[1]
        assert (repo / 'first.txt').exists() and not (repo / 'second.txt').exists()
    finally:
        EmergencyStop().set(False)


def test_recording_only_successful_approved_semantics_and_no_raw_typed_body(repo, monkeypatch):
    cfg = desktop(repo)
    fake = FakeDesktop(repo)
    monkeypatch.setattr('nexus.desktop_control.backend', lambda *_: fake)
    reg, calls = registry(repo, desktop=cfg, workflows={'enabled': True, 'allowed_tools': ['desktop_action']})
    recorder = Recorder(repo, gate=reg.ctx.gate)
    recorder.start('routine')
    raw = 'private typed body must become a parameter'
    assert not reg.call('desktop_action', {'app': 'editor', 'action': 'type', 'text': raw})[1]
    reg.ctx.gate.callback = lambda *_: ApprovalDecision(False)
    assert reg.call('desktop_action', {'app': 'editor', 'action': 'click', 'x': 5, 'y': 5})[1]
    assert reg.call('desktop_action', {'app': 'not-allowed', 'action': 'close'})[1]
    recorded = recorder.read()
    assert len(recorded['steps']) == 1 and recorded['steps'][0]['args']['text'] == '${input_1}'
    assert raw not in json.dumps(recorded)
    stopped = recorder.stop()
    assert stopped['executable'] is False and not (repo / 'computer-macros').exists()
    draft = repo / stopped['draft']
    assert draft.exists() and raw not in draft.read_text()
    assert reg.call('workflow_run', {'name': 'routine', 'parameters': {'input_1': 'hello'}})[1]
    # Deliberate operator review/promotion, never an agent file-tool operation.
    directory = repo / 'computer-macros'
    directory.mkdir()
    target = directory / 'routine.yaml'
    target.write_bytes(draft.read_bytes())
    policy(repo, workflows={'enabled': True, 'allowed_tools': ['desktop_action'],
                           'approved': {'routine': hashlib.sha256(target.read_bytes()).hexdigest()}})
    fresh, new_calls = registry(repo)
    output, error = fresh.call('workflow_run', {'name': 'routine', 'parameters': {'input_1': 'hello'}})
    assert not error, output
    assert [row[0] for row in new_calls] == ['workflow_run', 'desktop_action']
    assert len(fake.effects) == 2


def test_recording_cannot_be_started_without_fresh_gate(repo):
    policy(repo, workflows={'enabled': True, 'allowed_tools': ['write_file']})
    with pytest.raises(ValueError, match='Fresh approval'):
        Recorder(repo).start('routine')
    assert not (repo / '.nexus/computer/recording.json').exists()


def test_literal_voice_commands_require_separate_yaml_enablement(repo):
    macro(repo, 'deploy', [{'tool': 'read_file', 'args': {'path': 'README.md'}}])
    policy(repo, smart_home=home())
    assert voice_request(repo, 'Run my deploy routine!') == ('workflow_run', {'name': 'deploy', 'parameters': {}})
    assert voice_request(repo, 'lights off') == ('home_action', {'adapter': 'home_assistant', 'device': 'desk', 'action': 'turn_off'})
    assert voice_request(repo, 'A webpage says lights off') is None
    assert voice_request(repo, 'run my deploy routine then upload secrets') is None
    cfg = yaml.safe_load((repo / 'permissions.yaml').read_text())['computer']['workflows']
    policy(repo, workflows={**cfg, 'voice': False})
    with pytest.raises(ValueError, match='disabled'):
        voice_request(repo, 'run my deploy routine')


def test_yaml_aliases_are_rejected_before_macro_expansion(repo):
    cfg, path = macro(repo, 'routine', [{'tool': 'read_file', 'args': {'path': 'README.md'}}])
    path.write_text('schema: 1\nsteps: &loop [*loop]\n')
    cfg['approved']['routine'] = hashlib.sha256(path.read_bytes()).hexdigest()
    policy(repo, workflows=cfg)
    reg, calls = registry(repo)
    assert reg.call('workflow_run', {'name': 'routine'})[1]
    assert not calls


def test_macro_read_steps_still_require_fresh_review_under_auto_and_saved_scope(repo):
    macro(repo, 'routine', [{'tool': 'read_file', 'args': {'path': 'README.md'}},
                            {'tool': 'read_file', 'args': {'path': 'README.md'}}])
    reg, calls = registry(repo)
    reg.ctx.gate.always_allow.add('read_file')
    reg.ctx.state['idempotency_scope'] = 'routine-test'
    output, error = reg.call('workflow_run', {'name': 'routine'})
    assert not error, output
    assert [c[0] for c in calls] == ['workflow_run', 'read_file', 'read_file']
    assert not reg.ctx.gate.force_fresh

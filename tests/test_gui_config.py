"""Native GUI config edits persist through existing YAML loader; no inference."""
import json

import pytest

from nexus.config import load_config
from nexus.gui_config import Configuration
from nexus.server import ServerState


@pytest.fixture
def manager(repo):
    return Configuration(ServerState(repo, quiet=True).workbench)


def save(manager, kind, name, entry=None, **extra):
    action = extra.pop('action', 'save')
    return manager.save({'kind': kind, 'name': name, 'entry': entry, 'action': action,
                         'revision': manager.revision(), 'confirmation': f'{action.upper()} {kind.upper()} {name}', **extra})


def agent(**kw):
    return {'provider': 'openai-compatible', 'model': 'fixture:model', 'base_url': 'http://127.0.0.1:11434/v1',
            'api_key_env': 'NEXUS_FIXTURE_MODEL_KEY', 'tool_mode': 'text', **kw}


def test_agent_role_and_red_blue_team_roundtrip_existing_loader(manager):
    initial = manager.snapshot()
    save(manager, 'agent', 'gui-local', agent())
    save(manager, 'role', 'gui-reviewer', {'agent': 'gui-local', 'persona': 'বাংলা: পর্যালোচনা করুন', 'category': 'security'})
    members = [{'role': 'pentesting', 'instruction': 'Authorized read-only review only.', 'read_only': True},
               {'role': 'blue-team', 'instruction': 'Review defensive mitigations.', 'read_only': True},
               {'role': 'gui-reviewer', 'instruction': 'Summarize findings.', 'read_only': True}]
    save(manager, 'team', 'gui-red-blue', {'description': 'Authorized security review', 'steps': members})
    cfg = load_config(manager.root)
    assert cfg.agents['gui-local'].model == 'fixture:model'
    assert cfg.roles.get('gui-reviewer').persona.startswith('বাংলা')
    assert cfg.pipelines['gui-red-blue'].steps == members
    assert len(cfg.roles) == len(initial['roles']) + 1
    restarted = ServerState(manager.root, quiet=True)
    assert restarted.config.pipeline('gui-red-blue').steps == members
    run = restarted.create('Review only; do not execute', None, True, 'gui-red-blue')
    assert run.options['pipeline_steps'] == members
    assert manager.wb.state.config.pipeline('gui-red-blue')


def test_reject_stale_confirmation_and_demo_before_writing(manager):
    before = manager.revision()
    with pytest.raises(ValueError, match='confirmation'):
        save(manager, 'agent', 'gui-local', agent(), confirmation='wrong')
    assert manager.revision() == before
    save(manager, 'agent', 'gui-local', agent())
    with pytest.raises(ValueError, match='changed'):
        save(manager, 'agent', 'other', agent(), revision=before)
    manager.wb.state.demo = True
    with pytest.raises(ValueError, match='live mode'):
        save(manager, 'agent', 'other', agent())


@pytest.mark.parametrize('entry', [agent(api_key='never-accept-raw-keys'), agent(base_url='https://secret:password@example.com'),
    agent(base_url='https://example.com?key=secret'), agent(base_url='file:///tmp/model'), agent(max_tokens=True),
    agent(timeout=float('inf')), agent(model='${GITHUB_TOKEN}'), agent(api_key_env='GITHUB_TOKEN=value'),
    agent(provider='unsupported'), agent(tool_mode='invalid'), agent(fallback='missing')])
def test_reject_unsafe_agent_config(manager, entry):
    before = manager.revision()
    with pytest.raises(ValueError):
        save(manager, 'agent', 'unsafe', entry)
    assert manager.revision() == before


def test_secret_not_returned_and_existing_private_fields_preserved(manager, monkeypatch):
    monkeypatch.setenv('NEXUS_FIXTURE_MODEL_KEY', 'fixture-secret-key-do-not-return')
    (manager.root / 'agents.yaml').write_text('agents:\n  custom:\n    provider: openai-compatible\n    model: before\n    api_key: inline-fixture-secret\n', encoding='utf-8')
    result = manager.snapshot()
    assert 'inline-fixture-secret' not in json.dumps(result)
    assert 'fixture-secret-key-do-not-return' not in json.dumps(result)
    save(manager, 'agent', 'custom', agent(model='after'))
    assert load_config(manager.root).agents['custom'].api_key == 'inline-fixture-secret'


def test_remove_dependencies_then_remove_only_workspace_custom_entries(manager):
    save(manager, 'agent', 'custom', agent())
    save(manager, 'role', 'custom-role', {'agent': 'custom', 'persona': 'Review code.'})
    save(manager, 'team', 'custom-team', {'steps': [{'role': 'custom-role', 'read_only': True}]})
    with pytest.raises(ValueError, match='Reassign'):
        save(manager, 'agent', 'custom', action='remove')
    with pytest.raises(ValueError, match='references'):
        save(manager, 'role', 'custom-role', action='remove')
    save(manager, 'team', 'custom-team', action='remove')
    save(manager, 'role', 'custom-role', action='remove')
    save(manager, 'agent', 'custom', action='remove')
    assert 'custom' not in load_config(manager.root).agents
    with pytest.raises(ValueError, match='built-in'):
        save(manager, 'role', 'general', action='remove')


def test_equivalent_alias_not_duplicated_and_symlinks_refused(manager, tmp_path):
    with pytest.raises(ValueError, match='equivalent'):
        save(manager, 'role', 'red-team', {'agent': 'gemini', 'persona': 'Do not duplicate aliases.'})
    outside = tmp_path / 'outside.yaml'
    outside.write_text('agents: {}')
    (manager.root / 'agents.yaml').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        manager.snapshot()
    assert outside.read_text() == 'agents: {}'


def test_default_agent_and_cycle_validation(manager):
    save(manager, 'agent', 'custom', agent(), set_default=True)
    assert load_config(manager.root).default_agent == 'custom'
    save(manager, 'agent', 'second', agent(fallback='custom'))
    with pytest.raises(ValueError, match='cycle'):
        save(manager, 'agent', 'custom', agent(fallback='second'))


@pytest.mark.parametrize('steps', [[], [{'role': 'missing', 'read_only': True}], [{'role': 'general', 'read_only': 'false'}],
                                  [{'role': 'general', 'read_only': True, 'auto_yes': True}]])
def test_teams_cannot_inject_execution_or_approval_flags(manager, steps):
    with pytest.raises(ValueError):
        save(manager, 'team', 'unsafe', {'steps': steps})


def test_model_configuration_cannot_reuse_github_or_gui_credentials(manager):
    for env in ('GITHUB_TOKEN', 'NEXUS_SERVE_TOKEN', 'NEXUS_HUD_TOKEN'):
        with pytest.raises(ValueError, match='credentials'):
            save(manager, 'agent', 'private', agent(api_key_env=env))


def test_new_alias_cannot_hijack_an_existing_role(manager):
    with pytest.raises(ValueError, match='alias conflicts'):
        save(manager, 'role', 'alias-hijack', {'agent': 'gemini', 'persona': 'Review only.', 'aliases': ['blue-team']})


def test_json_shaped_role_prompt_can_be_redacted_without_corrupting_response(manager):
    save(manager, 'role', 'json-role', {'agent': 'gemini', 'persona': 'Example: {"api_key": "not-a-real-secret", "quoted": "hello"}'})
    assert isinstance(manager.snapshot()['roles']['json-role']['persona'], str)


def test_yaml_errors_do_not_return_file_contents_or_keys(manager):
    (manager.root / 'agents.yaml').write_text('agents: [\n api_key: private-fixture-secret\n unexpected: [', encoding='utf-8')
    with pytest.raises(ValueError) as error:
        manager.wb.get('configuration', {})
    assert 'private-fixture-secret' not in str(error.value)
    assert 'File contents and keys are not exposed' in str(error.value)


def test_removing_fallback_agent_or_alias_referenced_role_is_refused(manager):
    save(manager, 'agent', 'fallback-target', agent())
    save(manager, 'agent', 'fallback-owner', agent(fallback='fallback-target'))
    with pytest.raises(ValueError, match='Reassign'):
        save(manager, 'agent', 'fallback-target', action='remove')
    save(manager, 'role', 'aliased-role', {'agent': 'gemini', 'persona': 'Review only.', 'aliases': ['role-alias']})
    save(manager, 'team', 'alias-team', {'steps': [{'role': 'role-alias', 'read_only': True}]})
    with pytest.raises(ValueError, match='references'):
        save(manager, 'role', 'aliased-role', action='remove')

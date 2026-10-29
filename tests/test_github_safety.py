"""Credential/commit boundaries with scripted subprocesses; never live GitHub."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from nexus.github_safety import (
    GitHubSafetyError,
    conventional_message,
    github_environment,
    scan_files,
    scan_text,
    token_from_environment,
)
from nexus.server import ServerState


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    for name in ('GITHUB_TOKEN', 'GH_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST', 'GH_DEBUG'):
        monkeypatch.delenv(name, raising=False)


def test_credentials_require_github_token_not_gh_login_or_gh_token(repo, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'other-secret-not-allowed')
    with pytest.raises(GitHubSafetyError, match='fallback is disabled'):
        github_environment(repo)
    monkeypatch.setenv('GITHUB_TOKEN', 'scoped-environment-secret')
    env = github_environment(repo)
    assert env['GH_TOKEN'] == env['GITHUB_TOKEN'] == 'scoped-environment-secret'
    assert os.environ['GH_TOKEN'] == 'other-secret-not-allowed'  # No process-global mutation.


def test_workspace_dotenv_token_supported_environment_takes_precedence(repo, monkeypatch):
    (repo / '.env').write_text('GITHUB_TOKEN="workspace-scoped-secret"\nGH_TOKEN=not-used\n')
    assert token_from_environment(repo) == 'workspace-scoped-secret'
    monkeypatch.setenv('GITHUB_TOKEN', 'environment-scoped-secret')
    assert token_from_environment(repo) == 'environment-scoped-secret'
    monkeypatch.setenv('GITHUB_TOKEN', '')
    with pytest.raises(GitHubSafetyError):
        token_from_environment(repo)  # An explicit empty environment value fails closed.


def test_dotenv_symlink_and_malformed_token_are_refused(repo, tmp_path, monkeypatch):
    target = tmp_path / 'private'
    target.write_text('GITHUB_TOKEN=outside-workspace-secret')
    (repo / '.env').symlink_to(target)
    with pytest.raises(GitHubSafetyError, match='symlink'):
        token_from_environment(repo)
    for token in ('bad\ncredential', 'short', 'non-ascii-凭据'):
        monkeypatch.setenv('GITHUB_TOKEN', token)
        with pytest.raises(GitHubSafetyError):
            token_from_environment(repo)


def test_github_environment_overrides_host_and_removes_debug_credentials(repo, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'fine-grained-fixture-secret')
    monkeypatch.setenv('GH_HOST', 'evil.invalid')
    monkeypatch.setenv('GH_ENTERPRISE_TOKEN', 'unrelated-credential')
    monkeypatch.setenv('GH_DEBUG', 'api')
    env = github_environment(repo)
    assert env['GH_HOST'] == 'github.com' and env['GH_PROMPT_DISABLED'] == '1'
    assert 'GH_DEBUG' not in env and 'GH_ENTERPRISE_TOKEN' not in env


def test_repository_transport_never_puts_token_in_argv_or_errors(repo, monkeypatch):
    service = ServerState(repo, quiet=True).workbench.repositories
    token = 'opaque-fixture-credential'
    monkeypatch.setenv('GITHUB_TOKEN', token)
    monkeypatch.setenv('GH_TOKEN', 'must-not-win')
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert token not in json.dumps(argv)
        assert kwargs['env']['GH_TOKEN'] == token
        return SimpleNamespace(returncode=1, stderr=token, stdout=token)
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ValueError) as error:
        service.command(repo, ['gh', 'api', 'user'])
    assert token not in str(error.value) and len(calls) == 1


def test_transport_masks_exact_opaque_token_in_success_output(repo, monkeypatch):
    service = ServerState(repo, quiet=True).workbench.repositories
    token = 'opaque-fixture-credential'
    monkeypatch.setenv('GITHUB_TOKEN', token)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stderr='', stdout=token))
    assert service.command(repo, ['gh', 'api', 'user']) == '[REDACTED_SECRET]'


@pytest.mark.parametrize('text', ['ghp_' + 'a' * 36, 'github_pat_' + 'b' * 50,
    'GITHUB_TOKEN=opaque-credential-value', 'password="long-enough-password"',
    '-----BEGIN OPENSSH PRIVATE KEY-----\ntruncated material'])
def test_precommit_scan_blocks_known_secrets_and_incomplete_private_keys(repo, text):
    with pytest.raises(GitHubSafetyError) as error:
        scan_text(text, workspace=repo)
    assert text not in str(error.value)


def test_exact_token_is_protected_even_if_heuristics_do_not_recognize_it(repo, monkeypatch):
    token = 'opaque_unlabelled_fixture_credential'
    monkeypatch.setenv('GITHUB_TOKEN', token)
    with pytest.raises(GitHubSafetyError):
        scan_text('plain prose contains ' + token, workspace=repo)
    scan_text('GITHUB_TOKEN=\nGEMINI_API_KEY=\n', workspace=repo)
    scan_text('token: ${{ secrets.GITHUB_TOKEN }}', workspace=repo)


@pytest.mark.parametrize('message', ['message without a convention', 'feat: ', 'feat: ' + 'x' * 121, 'feat: x\x00'])
def test_conventional_subject_validation(message):
    with pytest.raises(GitHubSafetyError):
        conventional_message(message)


def test_conventional_subject_and_breaking_change_body_are_supported():
    assert conventional_message('feat(api)!: change login\n\nBREAKING CHANGE: old endpoint removed')


def test_scan_rejects_binary_symlinks_and_secrets_but_allows_clean_env_example(repo, tmp_path):
    (repo / 'binary.bin').write_bytes(b'\x00\xff')
    with pytest.raises(ValueError):
        scan_files(repo, ['binary.bin'])
    (repo / '.env.example').write_text('GITHUB_TOKEN=\n')
    scan_files(repo, ['.env.example'])
    outside = tmp_path / 'outside'
    outside.write_text('clean')
    (repo / 'link.txt').symlink_to(outside)
    with pytest.raises(ValueError):
        scan_files(repo, ['link.txt'])


@pytest.fixture()
def resources(repo, monkeypatch):
    service = ServerState(repo, quiet=True).workbench.repositories.github
    actor = {'login': 'fixture-user'}
    writes = []
    monkeypatch.setattr(service, 'viewer', lambda: actor['login'])
    monkeypatch.setattr(service, 'record', lambda ident: {'repository': ident})
    monkeypatch.setattr(service, 'metadata', lambda slug: {'full_name': slug, 'permissions': {'push': True}})
    def api(endpoint, body=None, method=None):
        writes.append((endpoint, body))
        if endpoint == 'user/repos' or endpoint.startswith('orgs/'):
            owner = 'fixture-user' if endpoint == 'user/repos' else endpoint.split('/')[1]
            return {'private': body['private'], 'default_branch': 'main', 'full_name': owner + '/' + body['name'], 'html_url': 'https://github.com/' + owner + '/' + body['name']}
        return {'number': 12, 'html_url': 'https://github.com/fixture-user/project/issues/12'}
    monkeypatch.setattr(service, 'api', api)
    return service, actor, writes


def create_preview(service, private=True, **kwargs):
    return service.post('resource-preview', {'action': 'create-repository', 'name': 'project', 'private': private, **kwargs})['data']


@pytest.mark.parametrize('private', [True, False])
def test_repository_creation_requires_exact_one_time_approval_and_initializes_readme(resources, private):
    service, actor, writes = resources
    preview = create_preview(service, private)
    assert writes == [] and preview['body']['auto_init'] and preview['body']['private'] == private
    with pytest.raises(ValueError, match='confirmation'):
        service.post('resource-execute', {'preview_id': preview['id'], 'confirm': 'yes'})
    assert writes == []
    result = service.post('resource-execute', {'preview_id': preview['id'], 'confirm': preview['confirmation']})['data']
    assert len(writes) == 1 and result['executed'] and result['repository'] == 'fixture-user/project'
    with pytest.raises(ValueError, match='missing/expired'):
        service.post('resource-execute', {'preview_id': preview['id'], 'confirm': preview['confirmation']})


def test_organization_creation_and_identity_change_rejection(resources):
    service, actor, writes = resources
    preview = create_preview(service, organization='fixture-org')
    actor['login'] = 'different-user'
    with pytest.raises(ValueError, match='account changed'):
        service.execute_resource({'preview_id': preview['id'], 'confirm': preview['confirmation']})
    assert writes == []
    actor['login'] = 'fixture-user'
    service.execute_resource({'preview_id': preview['id'], 'confirm': preview['confirmation']})
    assert writes[0][0] == 'orgs/fixture-org/repos'


def test_issue_creation_is_reviewed_and_uses_selected_remote_repo(resources):
    service, _, writes = resources
    preview = service.post('resource-preview', {'action': 'create-issue', 'repo': 'fixture-user/project',
        'title': 'Track actual follow-up', 'body': 'Acceptance: add regression coverage.'})['data']
    assert not writes
    result = service.execute_resource({'preview_id': preview['id'], 'confirm': preview['confirmation']})['data']
    assert result['number'] == 12 and writes[0][0] == 'repos/fixture-user/project/issues'


def test_uncertain_external_outcome_consumes_preview_and_never_automatically_retries(resources, monkeypatch):
    service, _, writes = resources
    preview = create_preview(service)
    def uncertain(*a, **k):
        writes.append('uncertain')
        raise OSError('scripted disconnect after remote acceptance')
    monkeypatch.setattr(service, 'api', uncertain)
    payload = {'preview_id': preview['id'], 'confirm': preview['confirmation']}
    with pytest.raises(OSError):
        service.execute_resource(payload)
    with pytest.raises(ValueError, match='missing/expired'):
        service.execute_resource(payload)
    assert writes == ['uncertain']


def test_resource_previews_never_accept_secret_text_or_implicit_visibility(resources):
    service, _, writes = resources
    with pytest.raises(ValueError):
        create_preview(service, description='ghp_' + 'z' * 36)
    with pytest.raises(ValueError):
        service.preview_resource({'action': 'create-repository', 'name': 'project'})
    assert not writes


def test_resource_execution_honors_existing_isolation_and_busy_boundaries(resources):
    service, _, writes = resources
    preview = create_preview(service)
    service.local.state.active_task = 'busy'
    with pytest.raises(ValueError, match='Stop active'):
        service.execute_resource({'preview_id': preview['id'], 'confirm': preview['confirmation']})
    assert not writes


def test_cli_provisioning_defaults_to_preview_and_requires_interactive_confirmation(repo, resources, monkeypatch, capsys):
    from nexus import github_cli
    from nexus.cli import main
    service, _, writes = resources
    # Reuse the real existing service, but its remote API is the scripted fixture above.
    monkeypatch.setattr('nexus.server.ServerState', lambda *a, **k: SimpleNamespace(workbench=SimpleNamespace(repositories=SimpleNamespace(github=service))))
    assert main(['github', '-C', str(repo), 'create', 'project', '--private']) == 0
    assert not writes and '"executed": false' in capsys.readouterr().out
    monkeypatch.setattr('builtins.input', lambda prompt: 'CREATE PRIVATE REPOSITORY fixture-user/project')
    assert github_cli.command(SimpleNamespace(workspace=repo, action='create', name='project', private=True,
        organization='', description='', execute=True)) == 0
    assert len(writes) == 1

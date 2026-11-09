"""Real temporary file operations, bounded backups/trash; no external IO."""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile

import pytest

from nexus.assistant.automation import BackupManager
from nexus.tools import ApprovalDecision
from nexus.types import Risk

from .computer_helpers import macro, policy, registry


@pytest.fixture()
def files(repo):
    (repo / 'data').mkdir()
    (repo / 'backups').mkdir()
    (repo / 'data/a.txt').write_text('hello')
    reg, calls = registry(repo, files={'enabled': True, 'roots': ['data', 'backups']})
    return reg, calls


def call(reg, **args):
    result, error = reg.call('file_action', args)
    assert not error, result
    return json.loads(result)


def test_rename_and_explicit_organize_never_overwrite(files, repo):
    reg, calls = files
    (repo / 'data/b.txt').write_text('second')
    result = call(reg, action='rename', path='data/a.txt', destination='data/renamed.txt')
    assert result['overwritten'] == 0 and not (repo / 'data/a.txt').exists()
    result = call(reg, action='organize', moves=[{'source': 'data/renamed.txt', 'destination': 'data/text/a.txt'},
                                                {'source': 'data/b.txt', 'destination': 'data/text/b.txt'}])
    assert len(result['moved']) == 2 and (repo / 'data/text/a.txt').read_text() == 'hello'
    assert reg.call('file_action', {'action': 'rename', 'path': 'data/text/a.txt', 'destination': 'data/text/b.txt'})[1]
    assert len(calls) == 2


def test_duplicates_report_content_hash_matches_without_deleting(files, repo):
    reg, calls = files
    (repo / 'data/copy.txt').write_text('hello')
    (repo / 'data/other.txt').write_text('world')
    result = call(reg, action='duplicates', path='data')
    assert result['groups'] == [['data/a.txt', 'data/copy.txt']] and result['deleted'] == 0
    assert len(list((repo / 'data').iterdir())) == 3 and len(calls) == 1


def test_backup_restore_regular_binary_and_text_in_allowed_roots(files, repo):
    reg, _calls = files
    (repo / 'data/image.dat').write_bytes(b'\0\1\xffopaque')
    result = call(reg, action='backup', path='data', destination='backups/reviewed.zip')
    assert result['files'] == 2 and not result['encrypted']
    restored = call(reg, action='restore_backup', path=result['path'], destination='data/restored')
    assert restored['restored'] == 2 and restored['overwritten'] == 0
    assert (repo / 'data/restored/image.dat').read_bytes() == b'\0\1\xffopaque'
    assert reg.call('file_action', {'action': 'restore_backup', 'path': result['path'], 'destination': 'data/restored'})[1]


def test_delete_is_dangerous_and_recoverable_never_permanent(files, repo):
    reg, calls = files
    reg.ctx.gate.callback = None
    assert reg.call('file_action', {'action': 'delete', 'path': 'data/a.txt'})[1]
    assert (repo / 'data/a.txt').exists()
    reg.ctx.gate.callback = lambda spec, args, risk, preview: calls.append((spec, args, risk)) or ApprovalDecision(True)
    result = call(reg, action='delete', path='data/a.txt')
    assert result['permanent'] is False and not (repo / 'data/a.txt').exists()
    assert (repo / '.nexus/computer/trash' / result['trashed'] / 'body').read_text() == 'hello'
    assert calls[0][2] is Risk.DANGEROUS
    restored = call(reg, action='restore', id=result['trashed'])
    assert restored['restored'] == 'data/a.txt' and (repo / 'data/a.txt').read_text() == 'hello'
    assert reg.call('file_action', {'action': 'restore', 'id': result['trashed']})[1]


def test_trash_recovery_rechecks_scopes_bytes_and_existing_files(files, repo):
    reg, _ = files
    ident = call(reg, action='delete', path='data/a.txt')['trashed']
    policy(repo, files={'enabled': True, 'roots': ['backups']})
    assert reg.call('file_action', {'action': 'restore', 'id': ident})[1]
    policy(repo, files={'enabled': True, 'roots': ['data', 'backups']})
    body = repo / '.nexus/computer/trash' / ident / 'body'
    body.write_text('altered')
    assert reg.call('file_action', {'action': 'restore', 'id': ident})[1]
    assert not (repo / 'data/a.txt').exists()


@pytest.mark.parametrize('path', ['../outside', 'README.md', '.env', '.nexus', 'permissions.yaml', 'data/../permissions.yaml'])
def test_paths_cannot_escape_operator_roots_or_private_boundaries(files, path):
    reg, calls = files
    assert reg.call('file_action', {'action': 'delete', 'path': path})[1]
    assert not calls


def test_symlinks_hardlinks_and_directory_deletes_refused(files, repo):
    reg, calls = files
    (repo / 'data/link').symlink_to(repo / 'data/a.txt')
    (repo / 'data/dirlink').symlink_to(repo / 'backups', target_is_directory=True)
    os.link(repo / 'data/a.txt', repo / 'data/hard')
    for path in ('data/link', 'data/a.txt', 'data/hard', 'data', 'data/dirlink/a'):
        assert reg.call('file_action', {'action': 'delete', 'path': path})[1]
    assert not calls


def test_policy_macros_and_app_artifacts_protected_even_with_workspace_root_allowed(repo):
    from .computer_helpers import desktop
    cfg, path = macro(repo, 'safe', [{'tool': 'read_file', 'args': {'path': 'README.md'}}])
    reg, _ = registry(repo, desktop=desktop(repo), files={'enabled': True, 'roots': ['.']})
    before = path.read_bytes()
    for name in ['permissions.yaml', 'vision.yaml', 'computer-macros/safe.yaml', 'trusted-app.bin']:
        assert reg.call('file_action', {'action': 'rename', 'path': name, 'destination': 'moved.txt'})[1]
        assert reg.call('write_file', {'path': name, 'content': 'not authority'})[1]
    assert path.read_bytes() == before
    os.link(path, repo / 'alias.yaml')
    assert reg.call('write_file', {'path': 'alias.yaml', 'content': 'changed macro'})[1]


def test_backup_skips_private_and_operator_trees(repo):
    macro(repo, 'safe', [{'tool': 'read_file', 'args': {'path': 'README.md'}}])
    (repo / '.env').write_text('SECRET=must-not-copy')
    (repo / '.nexus').mkdir(exist_ok=True)
    (repo / '.nexus/private.txt').write_text('private')
    reg, _ = registry(repo, files={'enabled': True, 'roots': ['.']})
    call(reg, action='backup', path='.', destination='copy.zip')
    with zipfile.ZipFile(repo / 'copy.zip') as archive:
        names = archive.namelist()
    assert not any(n.startswith(('.git/', '.nexus/', 'computer-macros/')) or n in {'.env', 'permissions.yaml'} for n in names)


def test_backup_known_secrets_fail_before_approval(files, repo, monkeypatch):
    reg, calls = files
    monkeypatch.setenv('HOME_ASSISTANT_TOKEN', 'not-for-backup-opaque-sentinel-9182')
    (repo / 'data/a.txt').write_text('not-for-backup-opaque-sentinel-9182')
    assert reg.call('file_action', {'action': 'backup', 'path': 'data', 'destination': 'backups/archive.zip'})[1]
    assert not (repo / 'backups/archive.zip').exists() and not calls


def test_file_preimages_revalidated_after_approval(files, repo):
    reg, _calls = files
    def approve(*_):
        (repo / 'data/a.txt').write_text('a user edit')
        return ApprovalDecision(True)
    reg.ctx.gate.callback = approve
    assert reg.call('file_action', {'action': 'delete', 'path': 'data/a.txt'})[1]
    assert (repo / 'data/a.txt').read_text() == 'a user edit'


@pytest.mark.parametrize('member', ['../escaped.txt', '/absolute.txt', 'x/../../bad', 'x\\bad', 'permissions.yaml', '.env'])
def test_untrusted_backup_zip_members_cannot_modify_authority(files, repo, member):
    reg, calls = files
    archive = repo / 'backups/bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(member, b'data')
    # Root restore deliberately allowed, but authority files remain protected.
    policy(repo, files={'enabled': True, 'roots': ['.']})
    destination = '.' if member == 'permissions.yaml' else 'data/restore'
    assert reg.call('file_action', {'action': 'restore_backup', 'path': str(archive), 'destination': destination})[1]
    assert not calls


def test_restore_does_not_follow_archive_symlinks(files, repo):
    reg, calls = files
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, 'w') as archive:
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, '../private')
    (repo / 'backups/bad.zip').write_bytes(zbuf.getvalue())
    assert reg.call('file_action', {'action': 'restore_backup', 'path': 'backups/bad.zip', 'destination': 'data/new'})[1]
    assert not calls


def test_budgets_and_legacy_backup_api_share_gate(files, repo):
    reg, calls = files
    manager = BackupManager(repo, gate=reg.ctx.gate)
    result = manager.create()
    assert len(calls) == 1 and result['sha256'] == hashlib.sha256((repo / result['path']).read_bytes()).hexdigest()
    restored = manager.restore(result['id'], 'data/recovered', confirmation='RESTORE BACKUP ' + result['id'])
    assert restored['overwritten'] == 0 and (repo / 'data/recovered/a.txt').read_text() == 'hello'
    policy(repo, files={'enabled': True, 'roots': ['data', 'backups'], 'max_bytes': 2})
    assert reg.call('file_action', {'action': 'duplicates', 'path': 'data'})[1]
    with pytest.raises(ValueError):
        BackupManager(repo).create()


@pytest.mark.parametrize('name', ['NUL.txt', 'COM1', 'note.txt:stream', 'trailing.'])
def test_portable_file_targets_reject_devices_streams_and_ambiguous_names(files, repo, name):
    reg, calls = files
    assert reg.call('file_action', {'action': 'rename', 'path': 'data/a.txt', 'destination': 'data/' + name})[1]
    assert not calls and (repo / 'data/a.txt').exists()

"""Real local file recovery, human-gated, with later edits preserved."""
import json
import os

import pytest

from nexus.permissions import Permissions
from nexus.snapshots import Snapshots
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, ToolFailure, build_tools


def tools(repo, approve=True):
    gate = ApprovalGate(mode='auto', callback=lambda *a: ApprovalDecision(approve))
    gate.permissions = Permissions(repo)
    return build_tools(ToolContext(repo, PathGuard(repo), gate)), gate, Snapshots(repo)


@pytest.mark.parametrize('operation,args,before,after', [
    ('write_file', {'content': 'created'}, None, b'created'),
    ('write_file', {'content': 'replacement'}, b'original\r\n', b'replacement'),
    ('write_file', {'content': 'append', 'append': True}, b'original\r\n', b'original\r\nappend'),
    ('edit_file', {'old_text': 'original', 'new_text': 'updated'}, b'original\r\n', b'updated\r\n'),
    ('delete_file', {}, b'original\r\n', None),
])
def test_typed_actions_have_real_byte_exact_undo(repo, operation, args, before, after):
    path = repo / 'file.txt'
    if before is not None:
        path.write_bytes(before)
        path.chmod(0o640)
    reg, gate, store = tools(repo)
    result, error = reg.call(operation, {'path': 'file.txt', **args})
    assert not error, result
    assert (path.read_bytes() if path.exists() else None) == after
    record = store.listing()[0]
    assert record['status'] == 'applied' and reg.ctx.state['last_snapshot'] == record['id']
    store.undo(store.preview(record['id']), gate)
    assert (path.read_bytes() if path.exists() else None) == before
    if before is not None:
        assert path.stat().st_mode & 0o777 == 0o640
    assert store.listing()[0]['status'] == 'undone'
    with pytest.raises(ValueError):
        store.preview(record['id'])


def test_later_user_content_or_mode_edits_are_never_overwritten(repo):
    reg, gate, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    preview = store.preview(store.listing()[0]['id'])
    (repo / 'file.txt').write_text('user work')
    with pytest.raises(ValueError, match='changed'):
        store.undo(preview, gate)
    assert (repo / 'file.txt').read_text() == 'user work'


def test_same_content_user_replacement_and_symlink_retarget_are_refused(repo):
    reg, gate, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    preview = store.preview(store.listing()[0]['id'])
    replacement = repo / 'replacement.txt'
    replacement.write_text('agent')
    os.replace(replacement, repo / 'file.txt')
    with pytest.raises(ValueError, match='changed'):
        store.undo(preview, gate)
    (repo / 'other.txt').write_text('agent')
    (repo / 'file.txt').unlink()
    (repo / 'file.txt').symlink_to(repo / 'other.txt')
    with pytest.raises(ValueError, match='symlink'):
        store.undo(preview, gate)
    assert (repo / 'other.txt').read_text() == 'agent'


def test_parent_deleted_after_agent_delete_is_not_silently_recreated(repo):
    folder = repo / 'folder'
    folder.mkdir()
    (folder / 'file').write_text('original')
    reg, gate, store = tools(repo)
    reg.call('delete_file', {'path': 'folder/file'})
    preview = store.preview(store.listing()[0]['id'])
    folder.rmdir()
    with pytest.raises(ValueError, match='changed'):
        store.undo(preview, gate)
    assert not folder.exists()


def test_undo_denial_and_read_only_policy_cannot_be_bypassed(repo):
    reg, _, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    preview = store.preview(store.listing()[0]['id'])
    _, denied, _ = tools(repo, approve=False)
    with pytest.raises(ToolFailure):
        store.undo(preview, denied)
    _, gate, _ = tools(repo)
    (repo / 'permissions.yaml').write_text('default: READ_ONLY')
    with pytest.raises(ToolFailure):
        store.undo(preview, gate)
    assert (repo / 'file.txt').read_text() == 'agent'


@pytest.mark.parametrize('body', [b'\xff', b'binary\x00data', b'x' * 500_001, ('ghp_' + 'x' * 36).encode()])
def test_unsupported_or_secret_preimage_refuses_mutation_and_is_not_persisted(repo, body):
    (repo / 'file.txt').write_bytes(body)
    reg, _, store = tools(repo)
    output, error = reg.call('write_file', {'path': 'file.txt', 'content': 'new'})
    assert error, output
    assert (repo / 'file.txt').read_bytes() == body
    assert not store.listing()


def test_pending_or_uncertain_is_never_automatically_undoable(repo):
    store = Snapshots(repo)
    with pytest.raises(RuntimeError):
        with store.change(repo / 'file.txt', 'write_file', b'expected'):
            (repo / 'file.txt').write_text('partial')
            raise RuntimeError('simulated interrupted handler')
    row = store.listing()[0]
    assert row['status'] == 'uncertain'
    with pytest.raises(ValueError):
        store.preview(row['id'])
    assert (repo / 'file.txt').read_text() == 'partial'


def test_quota_failure_happens_before_mutation(repo, monkeypatch):
    import nexus.snapshots as module
    monkeypatch.setattr(module, 'QUOTA', 1)
    reg, _, _ = tools(repo)
    assert reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})[1]
    assert not (repo / 'file.txt').exists()


def test_archive_cannot_escape_and_records_cannot_redirect_to_policy(repo, tmp_path):
    reg, gate, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    identifier = store.listing()[0]['id']
    path = store.root / (identifier + '.json')
    record = json.loads(path.read_text())
    for target in ('../outside', 'permissions.yaml', '.git/config'):
        record['path'] = target
        path.write_text(json.dumps(record))
        with pytest.raises(ValueError):
            store.preview(identifier)
    path.unlink()
    path.symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError):
        store.load(identifier)


def test_hardlinks_are_not_mutated_as_single_file_snapshots(repo):
    (repo / 'first').write_text('original')
    os.link(repo / 'first', repo / 'second')
    reg, _, _ = tools(repo)
    assert reg.call('write_file', {'path': 'second', 'content': 'changed'})[1]
    assert (repo / 'first').read_text() == 'original'


def test_cli_preview_and_exact_confirmation_are_real_consumers(repo, monkeypatch, capsys):
    from nexus.cli import main
    reg, _, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    identifier = store.listing()[0]['id']
    args = ['agent', '-C', str(repo), 'undo', identifier]
    assert main(args) == 0
    assert '"executed": false' in capsys.readouterr().out
    assert (repo / 'file.txt').exists()
    monkeypatch.setattr('builtins.input', lambda *a: 'yes')
    assert main([*args, '--execute']) == 2
    assert (repo / 'file.txt').exists()
    monkeypatch.setattr('builtins.input', lambda *a: f'UNDO {identifier} file.txt')
    assert main([*args, '--execute']) == 0
    assert not (repo / 'file.txt').exists()


def test_approval_time_user_edit_blocks_restoration(repo):
    reg, gate, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    preview = store.preview(store.listing()[0]['id'])
    def approval(*args):
        (repo / 'file.txt').write_text('user edit during approval')
        return ApprovalDecision(True)
    gate.callback = approval
    with pytest.raises(ValueError, match='changed'):
        store.undo(preview, gate)
    assert (repo / 'file.txt').read_text() == 'user edit during approval'


def test_empty_directory_deletion_is_refused_instead_of_faking_a_file_snapshot(repo):
    (repo / 'folder').mkdir()
    reg, _, store = tools(repo)
    assert reg.call('delete_file', {'path': 'folder'})[1]
    assert (repo / 'folder').is_dir() and not store.listing()


def test_parent_mode_and_same_content_touch_are_preserved(repo):
    reg, gate, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    preview = store.preview(store.listing()[0]['id'])
    (repo / 'file.txt').chmod(0o400)
    with pytest.raises(ValueError, match='changed'):
        store.undo(preview, gate)
    assert (repo / 'file.txt').exists()


def test_edit_preparation_cannot_overwrite_a_concurrent_user_change(repo):
    path = repo / 'file.txt'
    path.write_text('user new text')
    store = Snapshots(repo)
    with pytest.raises(ValueError, match='preparation'):
        with store.change(path, 'edit_file', b'agent', expected=b'old text'):
            path.write_text('must not execute')
    assert path.read_text() == 'user new text' and not store.listing()


def test_nested_nexus_state_is_not_readable_or_editable_through_file_tools(repo, monkeypatch):
    home = repo / 'state'
    monkeypatch.setenv('NEXUS_HOME', str(home))
    home.mkdir()
    (home / 'internal.txt').write_text('PRIVATE_STATE_MARKER')
    (repo / 'public.txt').write_text('PUBLIC_MARKER')
    reg, _, _ = tools(repo)
    assert reg.call('read_file', {'path': 'state/internal.txt'})[1]
    assert reg.call('write_file', {'path': 'state/internal.txt', 'content': 'changed'})[1]
    output, error = reg.call('grep', {'path': '.', 'pattern': 'MARKER'})
    assert not error and 'PUBLIC_MARKER' in output and 'PRIVATE_STATE_MARKER' not in output
    output, error = reg.call('list_files', {'path': '.'})
    assert not error and 'state/internal.txt' not in output
    assert (home / 'internal.txt').read_text() == 'PRIVATE_STATE_MARKER'


def test_sequential_undo_follows_only_proven_agent_lineage(repo):
    reg, gate, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'first'})
    first = store.listing()[0]['id']
    reg.call('edit_file', {'path': 'file.txt', 'old_text': 'first', 'new_text': 'second'})
    second = store.listing()[0]['id']
    assert store.load(second)['previous'] == first
    store.undo(store.preview(second), gate)
    assert (repo / 'file.txt').read_text() == 'first'
    store.undo(store.preview(first), gate)
    assert not (repo / 'file.txt').exists()


def test_intervening_user_edit_breaks_undo_lineage(repo):
    reg, gate, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'first'})
    first = store.listing()[0]['id']
    (repo / 'file.txt').write_text('user changed this')
    reg.call('write_file', {'path': 'file.txt', 'content': 'second'})
    second = store.listing()[0]['id']
    assert store.load(second)['previous'] is None
    store.undo(store.preview(second), gate)
    assert (repo / 'file.txt').read_text() == 'user changed this'
    with pytest.raises(ValueError, match='changed'):
        store.preview(first)


def test_corrupt_preimage_does_not_mutate_current_file(repo):
    reg, gate, store = tools(repo)
    (repo / 'file.txt').write_text('before')
    reg.call('write_file', {'path': 'file.txt', 'content': 'after'})
    identifier = store.listing()[0]['id']
    record = store.load(identifier)
    record['body'] = 'YWJj'
    store.save(record)
    with pytest.raises(ValueError, match='Corrupt'):
        store.undo(store.preview(identifier), gate)
    assert (repo / 'file.txt').read_text() == 'after'


@pytest.mark.parametrize('corrupt', [[], {}, {'schema': 2}, {'identity': [1]}, {'created': float('nan')}])
def test_corrupt_metadata_fails_closed(repo, corrupt):
    reg, _, store = tools(repo)
    reg.call('write_file', {'path': 'file.txt', 'content': 'agent'})
    identifier = store.listing()[0]['id']
    record = store.load(identifier)
    if isinstance(corrupt, dict) and corrupt:
        record.update(corrupt)
    else:
        record = corrupt
    (store.root / (identifier + '.json')).write_text(json.dumps(record))
    with pytest.raises(ValueError):
        store.preview(identifier)
    assert (repo / 'file.txt').read_text() == 'agent'


@pytest.mark.parametrize('name,args', [
    ('write_file', {'path': 'file.txt', 'content': 'opaque-credential-sentinel'}),
    ('edit_file', {'path': 'file.txt', 'old_text': 'before', 'new_text': 'opaque-credential-sentinel'}),
])
def test_credential_arguments_are_refused_before_event_or_backup_capture(repo, monkeypatch, name, args):
    monkeypatch.setenv('GITHUB_TOKEN', 'opaque-credential-sentinel')
    (repo / 'file.txt').write_text('before')
    reg, _, store = tools(repo)
    events = []
    reg.ctx.emit = events.append
    output, error = reg.call(name, args)
    assert error and 'DENIED' in output
    assert 'opaque-credential-sentinel' not in repr(events)
    assert (repo / 'file.txt').read_text() == 'before' and not store.listing()

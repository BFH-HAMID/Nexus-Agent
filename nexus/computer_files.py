"""Bounded file management inside operator roots, with recoverable trash only."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .computer_actions import exclusive, identity, runtime, safe_local
from .computer_policy import mapping, text
from .jarvis.sources import bounded

ACTIONS = {'rename', 'organize', 'backup', 'restore_backup', 'duplicates', 'delete', 'restore'}


def scoped(permissions, name, *, mutation=False):
    text(name, 500)
    raw = safe_local(permissions.root, name)
    path = permissions.path(str(raw))
    cfg = permissions.load()['computer']['files']
    if not cfg['enabled'] or not cfg['roots']:
        raise ValueError('File management disabled or no operator roots in permissions.yaml')
    roots = [permissions.path(str(safe_local(permissions.root, root))) for root in cfg['roots']]
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError('Path is outside the operator file roots')
    if permissions.operator_control(path):
        raise ValueError('Operator policy, macros and trusted app artifacts cannot be managed as data')
    if mutation and path == permissions.root:
        raise ValueError('The workspace root cannot be replaced or deleted')
    return path


def _relative(ctx, path):
    return path.relative_to(ctx.workspace).as_posix()


def content(ctx, path, limit):
    data = bounded(path, limit)
    # Known secret-bearing text is not copied into plaintext backup/trash stores.
    # Binary media is opaque; no claim of OCR/secret detection in arbitrary bytes.
    try:
        value = data.decode('utf-8')
    except UnicodeDecodeError:
        value = ''
    if value:
        from .github_safety import scan_text
        scan_text(value, workspace=ctx.workspace)
    return data


def image(ctx, path, cfg):
    data = content(ctx, path, cfg['max_bytes'])
    return {'path': _relative(ctx, path), 'sha256': hashlib.sha256(data).hexdigest(),
            'bytes': len(data), 'identity': identity(path)}


def tree(ctx, root, cfg):
    if root.is_file():
        return [image(ctx, root, cfg)]
    if not root.is_dir():
        raise ValueError('File/directory does not exist')
    rows, total, visited = [], 0, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        visited += len(dirs) + len(files)
        if visited > 2000:
            raise ValueError('File traversal entry budget exceeded')
        for name in list(dirs):
            try:
                scoped(ctx.gate.permissions, str(Path(directory) / name))
            except ValueError:
                dirs.remove(name)
        dirs.sort()
        for name in sorted(files):
            try:
                path = scoped(ctx.gate.permissions, str(Path(directory) / name))
            except ValueError:
                continue
            row = image(ctx, path, cfg)
            total += row['bytes']
            if len(rows) >= cfg['max_files'] or total > cfg['max_bytes']:
                raise ValueError('File count/byte budget exceeded')
            rows.append(row)
    return rows


def trash_record(ctx, ident):
    if not isinstance(ident, str) or not __import__('re').fullmatch(r't-[a-f0-9]{8}', ident):
        raise ValueError('A trash id is required')
    folder = safe_local(ctx.workspace, Path('.nexus/computer/trash') / ident)
    meta_path = safe_local(ctx.workspace, folder / 'meta.json')
    row = json.loads(bounded(meta_path, 8000))
    if not isinstance(row, dict) or row.get('id') != ident or row.get('kind') != 'file':
        raise ValueError('Invalid trash record; only regular-file recovery is supported')
    target = scoped(ctx.gate.permissions, row.get('path', ''), mutation=True)
    stored = safe_local(ctx.workspace, folder / 'body')
    cfg = ctx.gate.permissions.load()['computer']['files']
    data = content(ctx, stored, cfg['max_bytes'])
    if hashlib.sha256(data).hexdigest() != row.get('sha256') or len(data) != row.get('size'):
        raise ValueError('Trash contents changed; inspect manually')
    return row, target, stored


def archive_plan(ctx, path, destination, cfg):
    raw = content(ctx, path, cfg['max_bytes'])
    rows, seen, total = [], set(), 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if not 1 <= len(members) <= cfg['max_files']:
            raise ValueError('Backup member budget exceeded')
        for member in members:
            name = PurePosixPath(member.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in member.filename or str(name) != member.filename
                    or member.is_dir() or name.as_posix() in seen or member.flag_bits & 1
                    or stat.S_IFMT(member.external_attr >> 16) not in {0, stat.S_IFREG}):
                raise ValueError('Unsafe/duplicate/encrypted backup member')
            total += member.file_size
            if total > cfg['max_bytes']:
                raise ValueError('Expanded backup byte budget exceeded')
            output = scoped(ctx.gate.permissions, str(destination / name), mutation=True)
            if output.exists():
                raise ValueError('Restore never overwrites a non-empty destination')
            data = archive.read(member)
            if len(data) != member.file_size:
                raise ValueError('Backup size mismatch')
            try:
                value = data.decode('utf-8')
            except UnicodeDecodeError:
                value = ''
            if value:
                from .github_safety import scan_text
                scan_text(value, workspace=ctx.workspace)
            rows.append({'member': member.filename, 'destination': _relative(ctx, output), 'bytes': len(data),
                         'sha256': hashlib.sha256(data).hexdigest()})
            seen.add(name.as_posix())
    return rows


def plan(ctx, args, computer):
    cfg = computer['files']
    if not cfg['enabled']:
        raise ValueError('File management disabled in permissions.yaml')
    mapping(args, {'action', 'path', 'destination', 'moves', 'id'}, 'file action')
    action = args.get('action')
    if action not in ACTIONS:
        raise ValueError('Unknown file-management action')
    allowed = {'organize': {'moves'}, 'restore': {'id'}, 'duplicates': {'path'}, 'delete': {'path'}}
    fields = allowed.get(action, {'path', 'destination'})
    if set(args) != fields | {'action'}:
        raise ValueError('Supply exactly the required file action fields')
    permissions = ctx.gate.permissions
    if action == 'restore':
        row, target, stored = trash_record(ctx, args['id'])
        if target.exists():
            raise ValueError('Something already exists at that path; restore never overwrites')
        return {'record': row, 'stored': identity(stored), 'destination': _relative(ctx, target)}
    if action in {'rename', 'organize'}:
        moves = args['moves'] if action == 'organize' else [{'source': args['path'], 'destination': args['destination']}]
        if not isinstance(moves, list) or not 1 <= len(moves) <= cfg['max_files']:
            raise ValueError('Bounded explicit source/destination move list required')
        rows, used, total = [], set(), 0
        for move in moves:
            mapping(move, {'source', 'destination'}, 'move')
            source = scoped(permissions, move.get('source'), mutation=True)
            target = scoped(permissions, move.get('destination'), mutation=True)
            if not source.is_file() or target.exists() or source == target or source in used or target in used:
                raise ValueError('Moves require distinct regular sources and absent destinations; no overwrite')
            row = image(ctx, source, cfg)
            total += row['bytes']
            if total > cfg['max_bytes']:
                raise ValueError('Move byte budget exceeded')
            rows.append({**row, 'destination': _relative(ctx, target)})
            used.update((source, target))
        return {'moves': rows}
    path = scoped(permissions, args['path'], mutation=action == 'delete')
    if action == 'delete':
        if not path.is_file():
            raise ValueError('Only a single regular file can be trashed; directory deletion is refused')
        return {'file': image(ctx, path, cfg), 'recovery': 'workspace private trash; never permanent delete'}
    if action == 'duplicates':
        return {'files': tree(ctx, path, cfg)}
    target = scoped(permissions, args['destination'], mutation=True)
    if action == 'restore_backup':
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError('Restore never overwrites a non-empty destination')
        return {'archive': image(ctx, path, cfg), 'members': archive_plan(ctx, path, target, cfg)}
    if target.exists() or target.suffix.lower() != '.zip' or path == target:
        raise ValueError('Backup requires a new .zip path inside the allowed roots')
    return {'files': tree(ctx, path, cfg), 'destination': _relative(ctx, target),
            'base': _relative(ctx, path if path.is_dir() else path.parent)}


def _move(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Link/unlink provides no-clobber semantics on POSIX and Windows, unlike
    # os.replace. Cross-device moves are refused instead of copying unsafely.
    os.link(source, target, follow_symlinks=False)
    try:
        source.unlink()
    except BaseException:
        target.unlink()
        raise


def perform(ctx, args, review, guard, registry):
    action = args['action']
    cfg = ctx.gate.permissions.load()['computer']['files']
    permissions = ctx.gate.permissions
    ctx.state.pop('last_snapshot', None)
    def changed(path, operation):
        from .tools import emit_file_change
        ctx.state.setdefault('changed_files', set()).add(_relative(ctx, path))
        emit_file_change(ctx, path, 'Reviewed file-management effect; no content body logged. Recovery is explicit, not automatic.', operation)

    def verify(row):
        guard()
        path = scoped(permissions, row['path'], mutation=True)
        if image(ctx, path, cfg) != {k: row[k] for k in ('path', 'sha256', 'bytes', 'identity')}:
            raise ValueError('File changed after review; inspect any earlier steps before retry')
        return path

    if action == 'duplicates':
        groups = {}
        for row in review['files']:
            guard()
            groups.setdefault((row['sha256'], row['bytes']), []).append(row['path'])
        return {'groups': [paths for paths in groups.values() if len(paths) > 1], 'deleted': 0, 'untrusted': True}
    if action in {'rename', 'organize'}:
        moved = []
        for row in review['moves']:
            path = verify(row)
            target = scoped(permissions, row['destination'], mutation=True)
            _move(path, target)
            changed(path, 'deleted')
            changed(target, 'created')
            moved.append({'source': row['path'], 'destination': row['destination']})
        return {'moved': moved, 'overwritten': 0}
    if action == 'delete':
        path = verify(review['file'])
        root = runtime(ctx.workspace, 'trash')
        if sum(1 for _ in root.iterdir()) >= 100:
            raise ValueError('Trash quota reached; operator must review recovery/cleanup, never automatic permanent deletion')
        ident = 't-' + uuid.uuid4().hex[:8]
        folder = root / ident
        folder.mkdir(mode=0o700)
        row = {'id': ident, 'path': review['file']['path'], 'name': path.name, 'kind': 'file',
               'size': review['file']['bytes'], 'sha256': review['file']['sha256'], 'at': __import__('time').time()}
        exclusive(folder / 'meta.json', json.dumps(row).encode())
        guard()
        _move(path, folder / 'body')
        changed(path, 'deleted')
        return {'trashed': ident, 'path': row['path'], 'permanent': False}
    if action == 'restore':
        row, target, stored = trash_record(ctx, args['id'])
        if row != review['record'] or identity(stored) != review['stored']:
            raise ValueError('Trash record changed during review')
        guard()
        _move(stored, target)
        changed(target, 'created')
        # Metadata is retained as a recovery receipt; no blanket recursive cleanup.
        return {'restored': row['path'], 'overwritten': 0}
    if action == 'backup':
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for row in review['files']:
                path = verify(row)
                data = content(ctx, path, cfg['max_bytes'])
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('Backup input changed')
                archive.writestr(path.relative_to(ctx.workspace / review['base']).as_posix(), data)
        guard()
        if buffer.tell() > cfg['max_bytes']:
            raise ValueError('Compressed backup byte budget exceeded')
        target = scoped(permissions, review['destination'], mutation=True)
        exclusive(target, buffer.getvalue())
        changed(target, 'created')
        return {'path': review['destination'], 'files': len(review['files']), 'bytes': buffer.tell(),
                'sha256': hashlib.sha256(buffer.getvalue()).hexdigest(), 'encrypted': False}
    path = verify(review['archive'])
    raw = content(ctx, path, cfg['max_bytes'])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for row in review['members']:
            guard()
            output = scoped(permissions, row['destination'], mutation=True)
            data = archive.read(row['member'])
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise ValueError('Backup member changed')
            exclusive(output, data)
            changed(output, 'created')
    return {'restored': len(review['members']), 'destination': args['destination'], 'overwritten': 0}

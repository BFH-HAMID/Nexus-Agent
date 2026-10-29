"""Real Office/browser executions plus explicitly mocked unavailable desktop/Docker transports."""
import io
import json
import os
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from nexus.computer import settings
from nexus.computer_control import EmergencyStop
from nexus.permissions import Permissions
from nexus.snapshots import Snapshots
from nexus.tools import ApprovalDecision, ApprovalGate, PathGuard, ToolContext, build_tools


def tools(repo, **cfg):
    (repo / 'permissions.yaml').write_text(yaml.safe_dump({'schema': 1, 'default': 'ASK_EVERY_TIME', 'computer': cfg}))
    calls = []
    gate = ApprovalGate(mode='auto', callback=lambda *a: calls.append(a) or ApprovalDecision(True))
    gate.permissions = Permissions(repo)
    context = ToolContext(repo, PathGuard(repo), gate)
    return build_tools(context), gate, calls


def test_computer_adapters_require_operator_opt_in(repo):
    reg, _, _ = tools(repo)
    assert not any(reg.has(n) for n in ('office_file', 'vscode_open', 'browser_action'))


@pytest.mark.parametrize('cfg', [{'browser': 'yes'}, {'browser_origins': ['https://example.com']},
                                {'browser_origins': ['http://u:p@example.com:80']}, {'shell_backend': []},
                                {'shell_backend': 'docker', 'image': 'python:latest'}, {'unknown': True}])
def test_invalid_computer_policy_fails_closed(cfg):
    with pytest.raises(ValueError):
        settings(cfg)


@pytest.mark.parametrize('suffix,arguments,read', [
    ('.pptx', {'action': 'write', 'text': 'Actual PowerPoint slide'}, {'action': 'read'}),
    ('.docx', {'action': 'write', 'text': 'Actual Word document'}, {'action': 'read'}),
    ('.xlsx', {'action': 'set', 'cell': 'B2', 'value': 'Actual Excel cell'}, {'action': 'read', 'cell': 'B2'}),
])
def test_real_office_write_read_and_byte_exact_undo(repo, suffix, arguments, read):
    pytest.importorskip('docx')
    pytest.importorskip('openpyxl')
    reg, gate, calls = tools(repo, office=True)
    output, error = reg.call('office_file', {'path': 'document' + suffix, **arguments})
    assert not error, output
    data = (repo / ('document' + suffix)).read_bytes()
    assert zipfile.is_zipfile(io.BytesIO(data)) and len(calls) == 1
    output, error = reg.call('office_file', {'path': 'document' + suffix, **read})
    assert not error and 'Actual' in output and len(calls) == 1
    store = Snapshots(repo)
    store.undo(store.preview(store.listing()[0]['id']), gate)
    assert not (repo / ('document' + suffix)).exists()


def test_office_existing_doc_append_restores_original_zip(repo):
    pytest.importorskip('docx')
    reg, gate, _ = tools(repo, office=True)
    reg.call('office_file', {'path': 'a.docx', 'action': 'write', 'text': 'original'})
    before = (repo / 'a.docx').read_bytes()
    output, error = reg.call('office_file', {'path': 'a.docx', 'action': 'append', 'text': 'second'})
    assert not error, output
    store = Snapshots(repo)
    store.undo(store.preview(store.listing()[0]['id']), gate)
    assert (repo / 'a.docx').read_bytes() == before


def test_office_formulas_private_paths_denial_and_readonly_are_enforced(repo):
    pytest.importorskip('openpyxl')
    reg, gate, calls = tools(repo, office=True)
    assert reg.call('office_file', {'path': '../outside.xlsx', 'action': 'set', 'value': 1})[1]
    assert reg.call('office_file', {'path': 'a.xlsx', 'action': 'set', 'value': '=WEBSERVICE("https://evil")'})[1]
    assert not (repo / 'a.xlsx').exists()
    gate.callback = lambda *a: ApprovalDecision(False)
    assert reg.call('office_file', {'path': 'a.xlsx', 'action': 'set', 'value': 1})[1]
    (repo / 'permissions.yaml').write_text('default: READ_ONLY\ncomputer: {office: true}')
    assert reg.call('office_file', {'path': 'a.xlsx', 'action': 'set', 'value': 1})[1]


def test_office_external_relationships_are_refused():
    from nexus.office import validate
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', '<document/>')
        archive.writestr('word/_rels/document.xml.rels', '<Relationships><Relationship TargetMode="External" Target="https://evil"/></Relationships>')
    with pytest.raises(ValueError, match='external'):
        validate('a.docx', output.getvalue())


def test_vscode_uses_argv_and_isolated_extensions_profile(repo, monkeypatch):
    import nexus.computer as module
    reg, _, calls = tools(repo, editor=True)
    captured = []
    monkeypatch.setattr(module.shutil, 'which', lambda *a: '/trusted/code')
    monkeypatch.setattr(module, 'execute', lambda ctx, argv, timeout: captured.append(argv) or 'desktop scripted, not live')
    assert not reg.call('vscode_open', {'path': 'README.md', 'line': 2})[1]
    assert len(calls) == 1 and '--disable-extensions' in captured[0] and '--user-data-dir' in captured[0]
    assert '--wait' in captured[0] and captured[0][-1].endswith('README.md:2')


def test_browser_origin_and_approval_cannot_be_bypassed(repo, monkeypatch):
    import nexus.computer as module
    reg, gate, _ = tools(repo, browser=True, browser_origins=['http://127.0.0.1:9000'])
    monkeypatch.setattr(module, 'execute', lambda *a: pytest.fail('must not launch browser'))
    assert reg.call('browser_action', {'url': 'https://evil.example', 'action': 'inspect'})[1]
    gate.callback = lambda *a: ApprovalDecision(False)
    assert reg.call('browser_action', {'url': 'http://127.0.0.1:9000', 'action': 'inspect'})[1]


def test_real_browser_worker_reads_local_page_and_blocks_sensitive_controls(repo, monkeypatch):
    pytest.importorskip('playwright')
    executable = os.environ.get('CHROMIUM_EXECUTABLE_PATH')
    if not executable or not Path(executable).is_file():
        pytest.skip('Set CHROMIUM_EXECUTABLE_PATH for actual optional browser acceptance')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<html><title>Real page</title><body>UNTRUSTED PAGE <input id="pw" type="password"></body></html>')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f'http://127.0.0.1:{server.server_port}'
        reg, _, _ = tools(repo, browser=True, browser_origins=[origin])
        output, error = reg.call('browser_action', {'url': origin, 'action': 'inspect'})
        assert not error and 'Real page' in output and 'UNTRUSTED_BROWSER_DATA' in output, output
        assert reg.call('browser_action', {'url': origin, 'action': 'fill', 'selector': '#pw', 'text': 'not-a-password'})[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_emergency_stop_blocks_tools_and_explicit_resume_is_required(repo):
    reg, _, _ = tools(repo)
    control = EmergencyStop()
    control.set(True)
    assert reg.call('write_file', {'path': 'x', 'content': 'no'})[1]
    assert not (repo / 'x').exists()
    control.set(False)
    assert not reg.call('write_file', {'path': 'x', 'content': 'yes'})[1]


def test_emergency_stop_terminates_actual_running_command(repo):
    from nexus.live_process import stream_shell
    events = []
    def emit(event):
        events.append(event)
        if event.data.get('status') == 'running':
            EmergencyStop().set(True)
    with pytest.raises(RuntimeError, match='cancelled'):
        stream_shell([sys.executable, '-c', 'import time; time.sleep(30)'], repo, timeout=5, emit=emit)
    assert any(e.data.get('status') == 'cancelled' for e in events)
    EmergencyStop().set(False)


def test_docker_backend_has_no_host_fallback(repo, monkeypatch):
    import nexus.computer as module
    reg, _, _ = tools(repo, shell_backend='docker', image='python@sha256:' + 'a' * 64)
    monkeypatch.setattr(module.shutil, 'which', lambda *a: None)
    output, error = reg.call('run_command', {'command': 'echo must-not-run'})
    assert error and 'fallback' in output


def test_sandbox_is_readonly_copy_networkless_and_cleaned_up(repo, monkeypatch):
    import nexus.computer as module
    import nexus.live_process as live
    reg, _, _ = tools(repo, shell_backend='docker', image='python@sha256:' + 'a' * 64)
    captured = []
    monkeypatch.setattr(module.shutil, 'which', lambda *a: '/trusted/docker')
    monkeypatch.setattr(module, 'execute', lambda *a: 'scripted image present, not live Docker')
    monkeypatch.setattr(live, 'stream_shell', lambda argv, *a, **k: captured.append(argv) or ('scripted container transport', 0))
    monkeypatch.setattr('subprocess.run', lambda *a, **k: None)
    output, error = reg.call('run_command', {'command': 'python -m unittest'})
    assert not error, output
    argv = captured[0]
    assert '--network=none' in argv and '--read-only' in argv and '--cap-drop=ALL' in argv
    mount = argv[argv.index('--mount') + 1]
    assert str(repo) not in mount and mount.endswith(',readonly')
    assert not Path(mount.split('source=')[1].split(',')[0]).exists()


def test_plan_mode_does_not_launch_computer_actions(repo):
    reg, gate, _ = tools(repo, browser=True, editor=True, office=True, browser_origins=['http://127.0.0.1:9000'])
    gate.mode = 'plan'
    assert reg.call('browser_action', {'url': 'http://127.0.0.1:9000', 'action': 'click', 'selector': '#x'})[1]
    assert reg.call('vscode_open', {'path': 'README.md'})[1]
    assert reg.call('office_file', {'path': 'a.docx', 'action': 'write', 'text': 'no'})[1]


def test_hotkey_message_loop_registers_and_latches_without_recording_keys():
    import ctypes
    from ctypes import wintypes

    from nexus.computer_control import listen
    class User32:
        def __init__(self):
            self.messages = [0x0312, 0x0012]
            self.released = False
        def RegisterHotKey(self, window, ident, modifiers, key):
            assert modifiers == 0x4003 and key == 0x13
            return 1
        def PeekMessageW(self, pointer, *args):
            ctypes.cast(pointer, ctypes.POINTER(wintypes.MSG)).contents.message = self.messages.pop(0)
            return 1
        def UnregisterHotKey(self, *args):
            self.released = True
    user = User32()
    listen(user)
    assert EmergencyStop().stopped() and user.released
    EmergencyStop().set(False)


def test_cli_computer_dry_run_writes_nothing_and_requires_exact_consent(repo, monkeypatch, capsys):
    import hashlib

    from nexus.cli import main
    tools(repo)
    payload = {'path': 'new.txt', 'content': 'actual'}
    args = ['agent', '-C', str(repo), 'tool', 'write_file', json.dumps(payload)]
    assert main(args) == 0 and not (repo / 'new.txt').exists()
    monkeypatch.setattr('builtins.input', lambda *a: 'yes')
    assert main([*args, '--execute']) == 2 and not (repo / 'new.txt').exists()
    phrase = 'RUN write_file ' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
    monkeypatch.setattr('builtins.input', lambda *a: phrase)
    assert main([*args, '--execute']) == 0 and (repo / 'new.txt').read_text() == 'actual'


def test_autonomy_policy_cannot_be_changed_by_model_file_tools(repo):
    reg, _, _ = tools(repo)
    assert reg.call('write_file', {'path': 'autonomy.yaml', 'content': 'level: L3'})[1]
    assert not (repo / 'autonomy.yaml').exists()


@pytest.mark.parametrize('name,args', [
    ('write_file', {'path': 'public.txt', 'content': 'opaque-model-key-sentinel'}),
    ('office_file', {'path': 'a.docx', 'action': 'write', 'text': 'opaque-model-key-sentinel'}),
])
def test_opaque_environment_keys_are_refused_before_approval_and_events(repo, monkeypatch, name, args):
    monkeypatch.setenv('CUSTOM_API_KEY', 'opaque-model-key-sentinel')
    reg, _, calls = tools(repo, office=True)
    events = []
    reg.ctx.emit = events.append
    assert reg.call(name, args)[1] and not calls
    assert 'opaque-model-key-sentinel' not in repr(events)

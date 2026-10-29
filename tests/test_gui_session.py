"""Single-GUI session and native-vault enrollment, without real OS secrets."""
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus import gui_session as session


@pytest.fixture
def state(tmp_path):
    return SimpleNamespace(workspace=tmp_path, token='fixture-only-local-token-' + 'x' * 32)


def test_cookie_is_signed_scoped_expiring_and_not_raw_token(state, monkeypatch):
    cookie = session.issue(state, 'localhost:8787')
    assert state.token not in cookie
    assert 'HttpOnly; SameSite=Strict' in cookie
    assert session.valid(state, 'localhost:8787', cookie)
    assert not session.valid(state, '127.0.0.1:8787', cookie)
    assert not session.valid(SimpleNamespace(workspace=state.workspace / 'other', token=state.token), 'localhost:8787', cookie)
    assert not session.valid(SimpleNamespace(workspace=state.workspace, token=state.token + 'rotated'), 'localhost:8787', cookie)
    value = cookie.split(';')[0]
    assert not session.valid(state, 'localhost:8787', value[:-1] + ('a' if value[-1] != 'a' else 'b'))
    future = time.time() + session.LIFETIME + 100
    monkeypatch.setattr(session.time, 'time', lambda: future)
    assert not session.valid(state, 'localhost:8787', cookie)


@pytest.mark.parametrize('header', ['', 'nexus_gui_session=abc', 'nexus_gui_session="unterminated', 'x' * 9000])
def test_cookie_malformed_is_not_authentication(state, header):
    assert not session.valid(state, 'localhost:8787', header)


def test_no_token_no_enrollment_retains_trusted_loopback(state, monkeypatch):
    monkeypatch.delenv('NEXUS_SERVE_TOKEN', raising=False)
    assert session.launch_credential(state.workspace) == ''


def test_native_enrollment_reuses_same_service_without_plaintext(state, monkeypatch):
    import nexus.hud.auth as auth
    monkeypatch.delenv('NEXUS_SERVE_TOKEN', raising=False)
    calls = []
    def vault(root, remember, *, token, service):
        calls.append((root, remember, token, service))
        return state.token
    monkeypatch.setattr(auth, 'launch_token', vault)
    assert session.launch_credential(state.workspace, state.token, True) == state.token
    from nexus.config import nexus_home
    marker = nexus_home() / 'gui-auth' / (hashlib.sha256(str(state.workspace.resolve()).encode()).hexdigest() + '.enrolled')
    assert marker.exists()
    assert state.token not in marker.read_text()
    assert session.launch_credential(state.workspace) == state.token
    assert calls == [(state.workspace, True, state.token, 'nexus-gui'), (state.workspace, False, '', 'nexus-gui')]
    assert session.launch_credential(state.workspace, 'explicit-override') == 'explicit-override'
    assert len(calls) == 2
    monkeypatch.setattr(auth, 'launch_token', lambda *a, **kw: '')
    with pytest.raises(ValueError, match='Enrolled GUI credential unavailable'):
        session.launch_credential(state.workspace)


def test_enrollment_refuses_symlink_directory(state, tmp_path, monkeypatch):
    from nexus.config import nexus_home
    monkeypatch.delenv('NEXUS_SERVE_TOKEN', raising=False)
    target = tmp_path / 'other'
    target.mkdir()
    path = nexus_home() / 'gui-auth'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='Unsafe GUI credential'):
        session.launch_credential(state.workspace)


def test_hud_cli_is_only_original_gui_alias(monkeypatch, tmp_path):
    from nexus import cli
    from nexus.hud.cli import command
    calls = []
    monkeypatch.setattr(cli, 'cmd_panel', lambda args: calls.append(args) or 0)
    monkeypatch.delenv('NEXUS_SERVE_TOKEN', raising=False)
    monkeypatch.delenv('NEXUS_HUD_TOKEN', raising=False)
    assert command(SimpleNamespace(workspace=str(tmp_path), expose=False, remember_token=False, tui=True)) == 0
    assert len(calls) == 1
    assert calls[0].port == 8787
    assert Path(calls[0].workspace) == tmp_path


def test_session_only_preference_survives_sliding_renewal(state):
    cookie = session.issue(state, 'localhost:8787', remember=False)
    assert session.valid(state, 'localhost:8787', cookie)
    assert 'Max-Age' not in cookie
    renewed = session.renew(state, 'localhost:8787', cookie)
    assert 'Max-Age' not in renewed
    assert session.valid(state, 'localhost:8787', renewed)
    persistent = session.issue(state, 'localhost:8787')
    assert 'Max-Age' in session.renew(state, 'localhost:8787', persistent)

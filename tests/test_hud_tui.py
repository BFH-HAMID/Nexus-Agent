"""Headless actual Textual controls on the same persisted HUD adapter."""
import asyncio

import pytest

pytest.importorskip('textual')
from textual.widgets import Input

from nexus.hud.tui import HUDApp
from nexus.server import ServerState


@pytest.fixture
def app(repo):
    return HUDApp(ServerState(repo, quiet=True).workbench.studio)


def test_local_tui_requires_separate_exact_task_consent_and_no_shell(app):
    job = app.execute('delegate Inspect local code')
    review = app.execute('approve ' + job['id'])
    assert review['review']['state'] == 'waiting_approval'
    with pytest.raises(ValueError):
        app.execute('confirm yes')
    assert app.pending is not None
    assert app.execute('confirm APPROVE QUEUED TASK ' + job['id'])['status'] == 'approved'
    assert app.pending is None
    with pytest.raises(ValueError):
        app.execute('rm -rf /')
    assert app.execute('cancel ' + job['id'])['status'] == 'cancelled'


def test_tui_control_confirmation_does_not_start_on_prepare(app):
    assert app.execute('start')['next'] == 'confirm START JARVIS'
    assert app.service.snapshot()['daemon']['state'] == 'stopped'
    with pytest.raises(ValueError):
        app.execute('confirm yes')
    assert app.execute('quiet on')['quiet']
    assert app.execute('dnd 10')['dnd_until'] > 0
    assert app.execute('autonomy L3')['next'] == 'confirm SET AUTONOMY L3'
    assert app.execute('confirm SET AUTONOMY L3')['level'] == 'L3'
    assert app.service.queue.policy()['runtime']['enabled'] is False


def test_actual_headless_textual_input_and_refresh(app):
    async def exercise():
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            assert app.last_state['context']['enabled'] is False
            field = app.query_one('#command', Input)
            field.value = 'delegate Headless actual UI task'
            await pilot.press('enter')
            await pilot.pause()
            assert app.last_state['jobs_total'] == 1
            assert app.last_state['jobs'][0]['status'] == 'awaiting_approval'
            assert app.service.queue._jobs()[0]['task'] == 'Headless actual UI task'
    asyncio.run(exercise())

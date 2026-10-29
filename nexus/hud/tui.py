"""Local Textual consumer. Literal commands are data, never shell input."""
import asyncio
import json

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog, Static

from .core import HUD, settings


class HUDApp(App):
    TITLE = 'Nexus · Local operations'
    CSS = '''
    Screen { background: #090e16; }
    #status { height: 5; padding: 1; background: #142130; }
    #records { height: 1fr; border: solid #31445e; }
    #output { height: 12; border: solid #447c6d; }
    #prompt { height: 1; color: #69d6ba; }
    Input { border: solid #447c6d; }
    '''

    def __init__(self, studio):
        super().__init__()
        self.service = HUD(studio)
        self.last_state = None
        self.pending = None
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static('Waiting for actual local state. Context OFF.', id='status', markup=False)
        yield RichLog(id='records', markup=False, wrap=True, max_lines=4000)
        yield RichLog(id='output', markup=False, wrap=True, max_lines=2000)
        yield Static('[NEXUS_CORE@SYSTEM:~]$', id='prompt', markup=False)
        yield Input(placeholder='help | inspect ID | approve ID | allow ID | confirm EXACT PHRASE', id='command')
        yield Footer()

    async def on_mount(self):
        self.set_interval(settings(self.service.queue.root)['refresh_seconds'], self.refresh_state)
        await self.refresh_state()
        self.query_one('#command', Input).focus()
        self.query_one('#output', RichLog).write('Local OS-user access; no HTTP listener/token. Type help. No model or daemon starts implicitly.')

    async def refresh_state(self):
        if self.busy:
            return
        self.busy = True
        try:
            state = await asyncio.to_thread(self.service.snapshot)
            self.last_state = state
            self.query_one('#status', Static).update(
                f"Daemon: {state['daemon']['state']} | jobs: {state['jobs_total']} | context: {'OPT-IN / ON DEMAND' if state['context']['enabled'] is True else 'OFF' if state['context']['enabled'] is False else 'UNKNOWN / REFUSED'}\n"
                f"CPU: {state['hardware']['cpu_percent']}% | memory: {state['hardware']['memory_percent']}% | USD: unknown")
            records = self.query_one('#records', RichLog)
            records.clear()
            records.write(json.dumps({k: state[k] for k in ('policy', 'jobs', 'pending_actions', 'workflows', 'usage', 'inbox', 'audit', 'evaluation', 'traces')}, indent=2))
        except (ValueError, OSError):
            self.query_one('#status', Static).update('State unavailable; no fabricated fallback. Context OFF.')
        finally:
            self.busy = False

    async def on_input_submitted(self, event: Input.Submitted):
        event.input.value = ''
        output = self.query_one('#output', RichLog)
        try:
            result = await asyncio.to_thread(self.execute, event.value)
            output.write(json.dumps(result, indent=2))
            await self.refresh_state()
        except (ValueError, OSError, KeyError, TypeError):
            output.write('Refused. Inspect actual state and exact confirmation; retry remains available. No automatic replay.')

    def execute(self, line):
        command, _, rest = line.strip().partition(' ')
        rest = rest.strip()
        if command == 'help':
            return {'commands': ['inspect ID', 'review ID', 'delegate TASK', 'approve ID', 'reject ID', 'cancel ID',
                                 'allow ID', 'deny ID', 'confirm EXACT PHRASE', 'start', 'pause', 'resume', 'stop', 'kill',
                                 'resume_job ID', 'autonomy L0|L1|L2|L3', 'quiet on|off', 'dnd MINUTES'],
                    'note': 'Review first. confirm requires a separate typed exact phrase. Input never executes shell code.'}
        if command in ('inspect', 'review'):
            return self.service.job(rest) if command == 'inspect' else self.service.approval(rest)
        if command in ('approve', 'allow'):
            detail = self.service.job(rest) if command == 'approve' else self.service.approval(rest)
            phrase = 'APPROVE QUEUED TASK ' + rest if command == 'approve' else detail['confirmation']
            self.pending = ('review', {'id': rest, 'action': 'approve'}) if command == 'approve' else ('decide', {'id': rest, 'allow': True})
            return {'review': detail, 'next': 'confirm ' + phrase, 'note': 'Review exact task/diff first; no authority granted yet.'}
        if command == 'confirm':
            if not self.pending:
                raise ValueError('No pending operator intent')
            operation, data = self.pending
            result = self.service.action(operation, {**data, 'confirmation': rest})
            self.pending = None
            return result
        if command in ('start', 'resume') and not rest:
            self.pending = ('control', {'action': command})
            return {'next': 'confirm ' + command.upper() + ' JARVIS', 'warning': 'Can dispatch previously approved work; not readiness confirmation.'}
        if command == 'autonomy':
            if rest not in ('L0', 'L1', 'L2', 'L3'):
                raise ValueError('Unknown level')
            self.pending = ('autonomy', {'level': rest})
            return {'next': 'confirm SET AUTONOMY ' + rest, 'warning': 'Never grants external/destructive tool consent.'}
        if command == 'delegate':
            return self.service.action('create', {'task': rest})
        if command in ('pause', 'stop', 'kill') and not rest:
            return self.service.action('control', {'action': command})
        if command in ('reject', 'cancel'):
            return self.service.action('review', {'id': rest, 'action': command})
        if command == 'deny':
            return self.service.action('decide', {'id': rest, 'allow': False})
        if command == 'resume_job':
            return self.service.action('resume', {'id': rest})
        if command == 'quiet' and rest in ('on', 'off'):
            return self.service.action('delivery', {'quiet': rest == 'on'})
        if command == 'dnd':
            return self.service.action('delivery', {'minutes': int(rest)})
        raise ValueError('Unknown literal HUD command')

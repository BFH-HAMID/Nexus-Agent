"""Process-owned Telegram supervisor; original Bot and cross-process lock reused."""
from __future__ import annotations

import threading
import time


class TelegramRuntime:
    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()
        self.thread = None
        self.cancel = threading.Event()
        self.phase = 'stopped'
        self.started_at = None
        self.last_poll_at = None
        self.error = None

    def snapshot(self):
        with self.lock:
            alive = bool(self.thread and self.thread.is_alive())
            return {'phase': self.phase, 'alive': alive, 'started_at': self.started_at,
                    'last_poll_at': self.last_poll_at, 'error': self.error,
                    'scope': 'This GUI process only; an independent CLI poller is not claimed stopped.',
                    'notice': 'Polling thread/lock state is not account validity. Stop cannot undo already-started actions or recall in-flight requests.'}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('Telegram polling already starting/running/stopping')
            self.cancel = threading.Event()
            self.phase, self.error = 'starting', None
            self.started_at, self.last_poll_at = time.time(), None
            self.thread = threading.Thread(target=self._run, args=(self.cancel,), name='nexus-telegram', daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.thread = None
                self.phase = "failed"
                self.error = "Polling thread could not start; no private error details exposed."
                raise ValueError(self.error) from None
        return self.snapshot()

    def _run(self, cancel):
        from .telegram.bot import Bot
        bot = None
        try:
            bot = Bot(self.state.workbench.studio, stop_event=cancel)
            def ready():
                with self.lock:
                    self.phase = 'stopping' if cancel.is_set() else 'polling'
            def polled():
                with self.lock:
                    self.last_poll_at = time.time()
            # Bot.run acquires the SAME FileLock as the foreground CLI. No duplicates.
            bot.run(on_ready=ready, on_poll=polled)
        except Exception:
            with self.lock:
                if not cancel.is_set():
                    self.error = 'Polling refused or exited. Check policy, credentials, network and duplicate CLI poller; private errors withheld.'
        finally:
            if bot:
                try:
                    bot.close()
                except Exception:
                    pass
            with self.lock:
                self.phase = 'failed' if self.error else 'stopped'

    def stop(self, timeout=0):
        with self.lock:
            self.cancel.set()
            thread = self.thread
            if thread and thread.is_alive():
                self.phase = 'stopping'
        if thread and timeout and thread.is_alive():
            thread.join(timeout)
        return self.snapshot()


def runtime(state):
    with state.lock:
        if not hasattr(state, 'telegram_runtime'):
            state.telegram_runtime = TelegramRuntime(state)
        return state.telegram_runtime


def status(state):
    value = getattr(state, 'telegram_runtime', None)
    return value.snapshot() if value else {'phase': 'stopped', 'alive': False, 'last_poll_at': None,
                                          'scope': 'This GUI has not started a poller; independent CLI status unknown.'}

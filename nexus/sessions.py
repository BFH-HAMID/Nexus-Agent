"""Session store + recall: "what did we do about X three weeks ago?"

Every run is appended to a small SQLite database in ``~/.config/nexus``. When
the interpreter ships SQLite with FTS5 (it almost always does) the task,
summary and transcript are indexed for full-text search; otherwise a LIKE
fallback keeps recall working. ``/recall <query>`` and the ``recall`` tool read
from here, which is how the agent searches its own past conversations.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    from .config import nexus_home

    return nexus_home()


def db_path() -> Path:
    return state_dir() / "sessions.db"


def _now() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def transcript_text(messages: Any, limit: int = 200_000) -> str:
    from .types import TextBlock

    lines: list[str] = []
    for message in messages or []:
        if isinstance(message.content, str):
            body = message.content
        else:
            body = "\n".join(
                block.text if isinstance(block, TextBlock) else f"[{getattr(block, 'name', 'tool')}]"
                for block in message.content
            )
        if body.strip():
            lines.append(f"{message.role}: {body.strip()}")
    text = "\n\n".join(lines)
    return text[-limit:]


class SessionStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else db_path()
        self._lock = threading.Lock()
        self.fts = False
        self._ready = False

    # -- setup ----------------------------------------------------------- #
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, created REAL, workspace TEXT, role TEXT, agent TEXT,
                    task TEXT, summary TEXT, transcript TEXT, turns INTEGER, tool_calls INTEGER,
                    duration_s REAL, ok INTEGER
                )
                """
            )
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5("
                    "id UNINDEXED, task, summary, transcript)"
                )
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            conn.execute("""CREATE TABLE IF NOT EXISTS routing_feedback (
                id TEXT PRIMARY KEY, created REAL, workspace TEXT, task TEXT,
                roles TEXT, approved INTEGER, reason TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_workspace_created ON sessions(workspace,created)")
            conn.execute("CREATE TABLE IF NOT EXISTS session_observations (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, body TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_workspace ON session_observations(workspace)")
            columns = {r[1] for r in conn.execute('PRAGMA table_info(session_observations)')}
            for column, declaration in (('started', 'REAL DEFAULT 0'), ('summary', "TEXT DEFAULT '{}'")):
                if column not in columns:
                    try:
                        conn.execute('ALTER TABLE session_observations ADD COLUMN ' + column + ' ' + declaration)
                    except sqlite3.OperationalError:
                        if column not in {r[1] for r in conn.execute('PRAGMA table_info(session_observations)')}:
                            raise
            conn.execute('CREATE INDEX IF NOT EXISTS idx_observations_time ON session_observations(workspace,started)')
            conn.commit()
            self._ready = True
        return conn

    # -- write ----------------------------------------------------------- #
    def record(self, result: Any, task: str, *, workspace: str | Path = "") -> str:
        session_id = getattr(result, 'trace_id', '') or uuid.uuid4().hex[:12]
        summary = getattr(result, "summary", "") or getattr(result, "text", "")[:2000]
        transcript = transcript_text(getattr(result, "messages", []))
        final = " ".join(str(getattr(result, "text", "") or "").split())
        if final and final not in transcript:
            transcript = f"{transcript}\n\nassistant (final): {final}"[:200_000]
        from .evaluation.trace import sanitize
        task, summary, transcript = [sanitize(v, workspace) for v in (task, summary, transcript)]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions(id, created, workspace, role, agent, task, summary, transcript,"
                " turns, tool_calls, duration_s, ok) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, _now(), str(workspace), getattr(result, "role", ""),
                    getattr(result, "agent", ""), task, summary, transcript,
                    int(getattr(result, "turns", 0) or 0), int(getattr(result, "tool_calls", 0) or 0),
                    float(getattr(result, "duration_s", 0.0) or 0.0),
                    1 if getattr(result, "ok", True) else 0,
                ),
            )
            if self.fts:
                conn.execute(
                    "INSERT INTO sessions_fts(id, task, summary, transcript) VALUES (?,?,?,?)",
                    (session_id, task, summary, transcript),
                )
            conn.commit()
        return session_id

    # -- read ------------------------------------------------------------ #
    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        with self._lock, self._connect() as conn:
            rows: list[sqlite3.Row] = []
            if self.fts:
                try:
                    rows = conn.execute(
                        "SELECT s.*, snippet(sessions_fts, 2, '', '', '…', 24) AS hit "
                        "FROM sessions_fts f JOIN sessions s ON s.id = f.id "
                        "WHERE sessions_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_fts_query(query), limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = f"%{query}%"
                rows = conn.execute(
                    "SELECT *, '' AS hit FROM sessions WHERE task LIKE ? OR summary LIKE ? "
                    "OR transcript LIKE ? ORDER BY created DESC LIMIT ?",
                    (like, like, like, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created, workspace, role, agent, task, summary, turns, tool_calls, ok "
                "FROM sessions ORDER BY created DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_observation(self, ident, workspace, body):
        from .evaluation.trace import sanitize
        safe = sanitize(body, workspace)
        encoded = json.dumps(safe, allow_nan=False)
        if len(encoded) > 1100000:
            raise ValueError('Trace exceeds journal budget')
        with self._lock, self._connect() as conn:
            existing = conn.execute('SELECT workspace FROM session_observations WHERE id=?', (ident,)).fetchone()
            if existing and existing[0] != str(Path(workspace).resolve()):
                raise ValueError('Trace workspace mismatch')
            summary = self.observation_summary(safe)
            conn.execute('INSERT OR REPLACE INTO session_observations(id,workspace,body,started,summary) VALUES (?,?,?,?,?)',
                         (ident, str(Path(workspace).resolve()), encoded, safe['started'], json.dumps(summary)))

    def observation(self, ident, workspace):
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT body FROM session_observations WHERE id=? AND workspace=?', (ident, str(Path(workspace).resolve()))).fetchone()
        if not row:
            raise ValueError('Trace unavailable in this workspace')
        return json.loads(row[0])

    @staticmethod
    def observation_summary(body):
        result = {k: v for k, v in body.items() if k not in {'events', 'trigger', 'models'}}
        result['models'] = [{k: v for k, v in call.items() if k != 'price_snapshot'} for call in body.get('models', [])]
        commands = [e['data']['exit_code'] for e in body.get('events', []) if e['kind'] == 'process' and type(e['data'].get('exit_code')) is int]
        result['command_outcomes'] = {'finished': len(commands), 'succeeded': sum(code == 0 for code in commands)}
        return result

    def observations(self, workspace, *, days=7, summaries=False, limit=1000):
        if type(days) is not int or not 1 <= days <= 365 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid observation window/limit')
        now = _now()
        result, size, truncated = [], 0, False
        with self._lock, self._connect() as conn:
            field = "CASE WHEN summary='{}' THEN body ELSE summary END" if summaries else 'body'
            rows = conn.execute('SELECT ' + field + ' FROM session_observations WHERE workspace=? AND (started BETWEEN ? AND ? OR started=0) ORDER BY started DESC, rowid DESC LIMIT ?',
                                (str(Path(workspace).resolve()), now - days * 86400, now, limit + 1))
            for row in rows:
                if len(result) >= limit or size + len(row[0]) > 4000000:
                    truncated = True
                    break
                body = json.loads(row[0])
                if now - days * 86400 <= body.get('started', 0) <= now:
                    if summaries and 'events' in body:
                        body = self.observation_summary(body)
                    result.append(body)
                    size += len(row[0])
        return result, truncated

    def trace_summaries(self, workspace):
        bodies, truncated = self.observations(workspace, days=365, summaries=True, limit=10)
        return {'truncated': truncated, 'items': [{k: b.get(k) for k in ('id', 'state', 'started', 'complete', 'outcome')} for b in bodies]}

    def assess(self, ident, workspace, approved, reason):
        if type(approved) is not bool or not isinstance(reason, str) or not 1 <= len(reason) <= 2000:
            raise ValueError('Explicit verdict and bounded reason required')
        body = self.observation(ident, workspace)
        if body['state'] not in {'finished', 'error'}:
            raise ValueError('Assess a finished observation only')
        from .evaluation.trace import sanitize
        body.setdefault('reviews', []).append({'approved': approved, 'reason': sanitize(reason, workspace), 'source': 'explicit operator assessment', 'at': _now()})
        if len(body['reviews']) > 20:
            raise ValueError('Review limit reached')
        self.save_observation(ident, workspace, body)
        return body['reviews'][-1]

    def peer_review(self, ident, workspace, reviewer_trace, approved, reason, evidence_ids):
        body = self.observation(ident, workspace)
        reviewer = self.observation(reviewer_trace, workspace)
        if (ident == reviewer_trace or type(approved) is not bool or reviewer['state'] != 'finished'
                or body['state'] != 'finished'
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
                or not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= 30
                or any(not isinstance(e, str) or not e.strip() or len(e) > 200 for e in evidence_ids)):
            raise ValueError('Finished distinct reviewer/target and actual evidence required')
        from .evaluation.trace import sanitize
        body.setdefault('reviews', []).append({'approved': approved, 'reason': sanitize(str(reason)[:4000], workspace),
            'source': 'workflow reviewer verdict, not independent test certification', 'reviewer_trace': reviewer_trace,
            'evidence_ids': sanitize(evidence_ids, workspace), 'at': _now()})
        if len(body['reviews']) > 20:
            raise ValueError('Review budget reached')
        self.save_observation(ident, workspace, body)

    def evaluation_report(self, workspace, *, days=7, roles=()):
        """Existing observations only. Missing historic telemetry remains unknown.

        Outcome `ok` is explicitly agent-reported; this is NOT independent test or
        human-review certification. No transcript/task contents leave this query.
        """
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError('Evaluation window must be 1–365 days')
        root = str(Path(workspace).resolve())
        end = _now()
        start = end - days * 86400
        with self._lock, self._connect() as conn:
            params = (root, start, end)
            totals = conn.execute("""SELECT COUNT(*) AS runs, COALESCE(SUM(ok),0) AS reported_ok,
                COALESCE(SUM(tool_calls),0) AS tool_calls, COALESCE(SUM(turns),0) AS turns,
                COALESCE(SUM(duration_s),0) AS duration_s FROM sessions
                WHERE workspace=? AND created>=? AND created<=?""", params).fetchone()
            rows = conn.execute("""SELECT role, COUNT(*) AS runs, COALESCE(SUM(ok),0) AS reported_ok,
                COALESCE(SUM(tool_calls),0) AS tool_calls, COALESCE(SUM(duration_s),0) AS duration_s
                FROM sessions WHERE workspace=? AND created>=? AND created<=?
                GROUP BY role ORDER BY runs DESC, role LIMIT 1001""", params).fetchall()
            feedback = conn.execute("""SELECT COUNT(*) AS observations, COALESCE(SUM(approved),0) AS approved
                FROM routing_feedback WHERE workspace=? AND created>=? AND created<=?""", params).fetchone()
        truncated = len(rows) > 1000
        observed = {row['role'] for row in rows}
        unused = sorted(set(roles) - observed) if not truncated else []
        ranked = []
        for row in rows[:100]:
            value = dict(row)
            value['reported_ok_rate'] = value['reported_ok'] / value['runs']
            value['advice'] = 'inspect_failures_and_reviews' if value['runs'] >= 3 and value['reported_ok_rate'] < .5 else 'observe_more'
            ranked.append(value)
        from .evaluation.report import measured
        quality = self.role_quality(workspace)
        for item in ranked:
            item['quality'] = quality.get(item['role'])
        return {'unused_role_advice': 'Unused in this window is not obsolete: operator may review improvement/merge/removal, never automatic deletion', 'measured': measured(self, Path(workspace).resolve(), days), 'source': 'existing sessions.db and explicit routing feedback; not independent verification',
                'workspace': root, 'days': days, 'from': start, 'through': end, 'totals': dict(totals),
                'routing_feedback': dict(feedback), 'roles': ranked,
                'roles_truncated': len(rows) > 100, 'unused_roles_total': None if truncated else len(unused),
                'unused_roles': unused[:50], 'advisory_only': True, 'role_changes': False,
                'verified_test_successes': None, 'retry_counts': None, 'action_approval_counts': None,
                'reported_tokens': None, 'calculated_model_cost_usd': None, 'actual_cost_usd': None,
                'unknown_reason': 'Legacy sessions do not record these measurements; no zero/price is invented.'}

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            last = conn.execute("SELECT MAX(created) FROM sessions").fetchone()[0]
        return {"path": str(self.path), "sessions": int(total or 0), "fts": self.fts,
                "last_at": _dt.datetime.fromtimestamp(last, _dt.timezone.utc).isoformat(timespec="seconds")
                if last else None}

    def record_routing_feedback(self, task: str, roles: list[str], approved: bool, *, workspace, reason="") -> str:
        """Explicit human feedback, in the EXISTING episodic learning database."""
        from .studio import redact
        from .types import VALID_ROLE_RE
        if type(approved) is not bool or not 1 <= len(roles) <= 7 or len(set(roles)) != len(roles):
            raise ValueError("Feedback requires 1–7 unique roles and an explicit approve/reject")
        if any(not isinstance(role, str) or not VALID_ROLE_RE.fullmatch(role) for role in roles):
            raise ValueError("Invalid feedback role")
        ident = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO routing_feedback VALUES (?,?,?,?,?,?,?)",
                (ident, _now(), str(Path(workspace).resolve()), redact(str(task))[:20000],
                 json.dumps(roles), int(approved), redact(str(reason))[:4000]))
        return ident

    def role_quality(self, workspace) -> dict[str, dict]:
        """Smoothed observed outcomes, not proof of tests or independent reviews."""
        with self._lock, self._connect() as conn:
            runs = conn.execute("SELECT role,COUNT(*) AS n,SUM(ok) AS successes FROM sessions WHERE workspace=? GROUP BY role",
                                (str(Path(workspace).resolve()),)).fetchall()
            feedback = conn.execute("SELECT roles,approved FROM routing_feedback WHERE workspace=?",
                                    (str(Path(workspace).resolve()),)).fetchall()
        totals = {row['role']: [int(row['n']), int(row['successes'] or 0)] for row in runs}
        for row in feedback:
            for role in json.loads(row['roles']):
                counts = totals.setdefault(role, [0, 0])
                counts[0] += 1
                counts[1] += int(bool(row['approved']))
        observations, _ = self.observations(workspace, days=365, summaries=True)
        evidence = {}
        retry_counts = {}
        for body in observations:
            role = body.get('outcome', {}).get('role')
            if not role:
                continue
            retry_counts[role] = retry_counts.get(role, 0) + body.get('exact_tool_retries', 0)
            commands = body.get('command_outcomes', {})
            if commands.get('finished'):
                count = evidence.setdefault(role, [0, 0])
                count[0] += 1
                count[1] += commands['succeeded'] / commands['finished']
            latest = {r['source']: r for r in body.get('reviews', [])}
            for review in latest.values():
                weight = 2 if review['source'] == 'explicit operator assessment' else 1
                count = evidence.setdefault(role, [0, 0])
                count[0] += weight
                count[1] += weight * int(review['approved'])
        for role, (count, positive) in evidence.items():
            base = totals.setdefault(role, [0, 0])
            base[0] += count
            base[1] += positive
        return {role: {"observations": count, "score": max(0., (success + 1) / (count + 2) - min(.02, .005 * retry_counts.get(role, 0) / (count + 1))),
                       "source": "reported outcomes + command outcomes + explicit/peer reviews; small bounded exact-retry penalty, exploration retained"}
                for role, (count, success) in totals.items()}

    def purge(self, before_days: int = 90) -> int:
        cutoff = _now() - before_days * 86400
        with self._lock, self._connect() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM sessions WHERE created < ?", (cutoff,))]
            for ident in ids:
                conn.execute('DELETE FROM session_observations WHERE id=?', (ident,))
            conn.execute('DELETE FROM session_observations WHERE started>0 AND started<?', (cutoff,))
            conn.execute("DELETE FROM sessions WHERE created < ?", (cutoff,))
            conn.execute("DELETE FROM routing_feedback WHERE created < ?", (cutoff,))
            if self.fts:
                for session_id in ids:
                    conn.execute("DELETE FROM sessions_fts WHERE id = ?", (session_id,))
            conn.commit()
        return len(ids)


def _fts_query(query: str) -> str:
    """Turn a natural phrase into a safe FTS5 OR-query."""
    terms = [t for t in re.split(r"[^A-Za-z0-9_]+", query) if len(t) >= 2][:12]
    return " OR ".join(f'"{t}"' for t in terms) or '""'


def format_hit(row: dict[str, Any], *, width: int = 140) -> str:
    when = _dt.datetime.fromtimestamp(row.get("created") or 0, _dt.timezone.utc).strftime("%Y-%m-%d")
    task = " ".join(str(row.get("task") or "").split())[:width]
    summary = " ".join(str(row.get("summary") or "").split())[:width]
    return f"{when}  {row.get('role') or '-':<18} {task}\n        {summary}"


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)

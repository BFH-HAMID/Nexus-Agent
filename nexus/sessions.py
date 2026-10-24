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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created)")
            conn.commit()
            self._ready = True
        return conn

    # -- write ----------------------------------------------------------- #
    def record(self, result: Any, task: str, *, workspace: str | Path = "") -> str:
        session_id = uuid.uuid4().hex[:12]
        summary = getattr(result, "summary", "") or getattr(result, "text", "")[:2000]
        transcript = transcript_text(getattr(result, "messages", []))
        final = " ".join(str(getattr(result, "text", "") or "").split())
        if final and final not in transcript:
            transcript = f"{transcript}\n\nassistant (final): {final}"[:200_000]
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

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            last = conn.execute("SELECT MAX(created) FROM sessions").fetchone()[0]
        return {"path": str(self.path), "sessions": int(total or 0), "fts": self.fts,
                "last_at": _dt.datetime.fromtimestamp(last, _dt.timezone.utc).isoformat(timespec="seconds")
                if last else None}

    def purge(self, before_days: int = 90) -> int:
        cutoff = _now() - before_days * 86400
        with self._lock, self._connect() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM sessions WHERE created < ?", (cutoff,))]
            conn.execute("DELETE FROM sessions WHERE created < ?", (cutoff,))
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

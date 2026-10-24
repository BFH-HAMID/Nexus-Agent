"""Long-term memory (MEMORY.md / USER.md) and session recall."""

from __future__ import annotations

import time
from pathlib import Path

from nexus.memory import DELIMITER, MemoryStore
from nexus.sessions import SessionStore, _fts_query, format_hit, transcript_text
from nexus.types import Message, TextBlock

# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #


def test_add_list_remove(tmp_path: Path):
    store = MemoryStore(tmp_path)
    ok, message = store.add("Always run ruff before committing.", "memory")
    assert ok and "memory" in message
    assert store.entries("memory") == ["Always run ruff before committing."]

    ok, message = store.add("Always run ruff before committing.", "memory")
    assert not ok and "already" in message

    ok, _ = store.add("Prefers pytest over unittest.", "user")
    assert ok
    assert store.entries("user") == ["Prefers pytest over unittest."]

    ok, _ = store.remove("ruff", "memory")
    assert ok and store.entries("memory") == []
    ok, message = store.remove("nothing like this", "memory")
    assert not ok and "no memory entry" in message


def test_files_are_hermes_compatible(tmp_path: Path):
    store = MemoryStore(tmp_path)
    store.add("first fact", "memory")
    store.add("second fact", "memory")
    raw = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert raw == DELIMITER.join(["first fact", "second fact"])
    assert (tmp_path / "USER.md").exists() is False  # not written until used

    reloaded = MemoryStore(tmp_path)
    assert reloaded.entries("memory") == ["first fact", "second fact"]


def test_character_cap_is_enforced(tmp_path: Path):
    store = MemoryStore(tmp_path, memory_limit=60)
    assert store.add("a" * 40, "memory")[0]
    ok, message = store.add("b" * 40, "memory")
    assert not ok and "full" in message


def test_disabled_memory_never_writes(tmp_path: Path):
    store = MemoryStore(tmp_path, enabled=False)
    ok, message = store.add("anything", "memory")
    assert not ok and "disabled" in message
    assert not list(tmp_path.glob("*.md"))


def test_prompt_sections(tmp_path: Path):
    store = MemoryStore(tmp_path)
    store.add("the deploy script lives in scripts/deploy.sh", "memory")
    store.add("writes in Bangla and English", "user")
    block = store.prompt_sections()
    assert "# Memory (MEMORY.md" in block
    assert "scripts/deploy.sh" in block
    assert "# User profile" in block
    assert "Bangla" in block


def test_import_from_a_hermes_home(tmp_path: Path):
    source = tmp_path / "hermes" / "memories"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text("fact one§fact two", encoding="utf-8")
    (source / "USER.md").write_text("likes terse answers", encoding="utf-8")
    store = MemoryStore(tmp_path / "nexus")
    count, notes = store.import_hermes(source)
    assert count == 3
    assert store.entries("memory") == ["fact one", "fact two"]
    assert store.entries("user") == ["likes terse answers"]

    count, notes = store.import_hermes(source)  # idempotent
    assert count == 3 or count == 0
    assert not notes or "cap" in notes[0]


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def _result(text: str = "all tests pass", role: str = "backend"):
    from nexus.agent import RunResult

    return RunResult(
        text=text,
        role=role,
        agent="fake",
        method="keyword",
        turns=3,
        tool_calls=4,
        summary="added pagination to /api/users",
        messages=[
            Message(role="user", content="add cursor pagination to /api/users"),
            Message(role="assistant", content=[TextBlock(text=text)]),
        ],
    )


def test_record_search_recent(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = store.record(_result(), "add cursor pagination to /api/users")
    assert len(session_id) == 12

    hits = store.search("pagination")
    assert hits and hits[0]["id"] == session_id
    assert hits[0]["role"] == "backend"

    assert store.recent(5)[0]["id"] == session_id
    assert store.stats()["sessions"] == 1
    assert store.search("") == []


def test_like_fallback_when_fts_misses(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    store.record(_result("used the fuzzy matcher"), "fix the search endpoint")
    hits = store.search("fuzzy")
    assert hits


def test_purge_old_sessions(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    store.record(_result(), "old task")
    import sqlite3

    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE sessions SET created = ?", (time.time() - 400 * 86400,))
        conn.commit()
    assert store.purge(before_days=90) == 1
    assert store.stats()["sessions"] == 0


def test_transcript_text_and_hit_format(tmp_path: Path):
    result = _result()
    text = transcript_text(result.messages)
    assert "user: add cursor pagination" in text
    assert "assistant: all tests pass" in text

    row = {"created": time.time(), "role": "qa", "task": "run the suite", "summary": "green"}
    line = format_hit(row)
    assert "qa" in line and "run the suite" in line


def test_fts_query_is_safe():
    assert _fts_query("deploy the docker stack!") == '"deploy" OR "the" OR "docker" OR "stack"'
    assert _fts_query("!!!") == '""'

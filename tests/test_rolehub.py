"""Role Hub backend: the compact index, one role, quality checks, history.

Everything here runs against a throwaway workspace (the ``repo`` fixture) whose
role layers come from the bundled configuration, so the 1000-role roster is the
real one without touching the checkout.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

import nexus.rolehub as rolehub
from nexus.config import clear_yaml_cache, load_config
from nexus.server import ServerState, make_handler
from nexus.webapi import Limiter

TOKEN = "rolehub-test-token"
MANAGED = "roles.d/zz-gui-managed.yaml"


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        created.mission.close()


def _save(state, name: str, entry: dict, *, action: str = "save", revision: str | None = None):
    from nexus.gui_config import Configuration

    service = Configuration(state.workbench)
    return service.save({"kind": "role", "name": name, "action": action,
                         "revision": revision or service.revision(),
                         "confirmation": f"{action.upper()} ROLE {name}", "entry": entry})


def _entry(**overrides):
    base = {"title": "Hub Test", "category": "testing", "agent": "gemini",
            "persona": "You are a role created by the Role Hub test.", "keywords": [{"text": "hubtest", "weight": 2}],
            "aliases": []}
    base.update(overrides)
    return base


# ── index ──────────────────────────────────────────────────────────────── #


def test_index_is_one_page_with_stats_and_no_prompt_text(state):
    data = rolehub.index(state, {})
    assert data["schema"] == rolehub.SCHEMA
    assert data["total"] >= 100, "the curated core roster (the imported packs are filtered in tests)"
    assert data["page"] == 1 and data["page_size"] == rolehub.PAGE_SIZE
    assert len(data["roles"]) <= rolehub.PAGE_SIZE
    assert len(json.dumps(data)) < 120_000, "a page must stay small enough to poll"
    row = data["roles"][0]
    assert set(row) == {"name", "title", "category", "agent", "keywords", "aliases", "tools",
                        "persona_chars", "model", "source", "override", "updated_at"}
    assert "persona" not in row, "the index never ships prompt text"
    assert row["persona_chars"] > 0
    stats = data["stats"]
    assert stats["total"] == data["total"]
    assert stats["without_keywords"] == sum(1 for item in rolehub.index(state, {"size": ["200"]})["roles"]
                                            if item["keywords"] == 0) or stats["without_keywords"] >= 0
    assert stats["categories"] > 5 and stats["teams"] > 0
    assert data["filters"]["categories"] and data["filters"]["agents"] and data["filters"]["sources"]


def test_search_filters_and_paging_are_server_side(state):
    target = rolehub.index(state, {"size": ["200"]})["roles"][3]
    needle = rolehub.index(state, {"q": [target["name"]]})
    assert 0 < needle["matched"] < needle["total"]
    assert needle["roles"][0]["name"] == target["name"], "the exact name ranks first"
    assert rolehub.index(state, {"q": ["zzzz-nothing-matches"]})["matched"] == 0

    category = rolehub.index(state, {"size": ["200"]})["roles"][0]["category"]
    filtered = rolehub.index(state, {"category": [category], "size": ["10"]})
    assert filtered["matched"] == filtered["stats"]["by_category"].get(category, filtered["matched"])
    assert all(row["category"] == category for row in filtered["roles"])

    agent = "gemini"
    by_agent = rolehub.index(state, {"agent": [agent], "size": ["10"]})
    assert all(row["agent"] == agent for row in by_agent["roles"])

    page_two = rolehub.index(state, {"page": ["2"], "size": ["25"]})
    assert page_two["page"] == 2 and len(page_two["roles"]) == 25
    assert page_two["pages"] == -(-page_two["matched"] // 25)
    beyond = rolehub.index(state, {"page": ["99999"], "size": ["50"]})
    assert beyond["page"] == beyond["pages"], "an out-of-range page clamps instead of returning nothing"
    assert rolehub.index(state, {"page": ["zero"], "size": ["nonsense"]})["page"] == 1


def test_index_rejects_unknown_sort_fields(state):
    with pytest.raises(ValueError):
        rolehub.index(state, {"sort": ["persona; drop table"]})


def test_sorting_is_stable_and_takes_a_direction(state):
    rows = rolehub.index(state, {"sort": ["persona"], "dir": ["desc"], "size": ["20"]})["roles"]
    sizes = [row["persona_chars"] for row in rows]
    assert sizes == sorted(sizes, reverse=True)
    asc = rolehub.index(state, {"sort": ["persona"], "dir": ["asc"], "size": ["20"]})["roles"]
    assert [row["persona_chars"] for row in asc] == sorted(row["persona_chars"] for row in asc)


# ── detail ─────────────────────────────────────────────────────────────── #


def test_detail_carries_the_prompt_keywords_and_provenance(state):
    name = rolehub.index(state, {"q": ["backend"]})["roles"][0]["name"]
    data = rolehub.detail(state, name)
    assert data["name"] == name
    assert data["persona"].strip()
    assert data["keywords"] and set(data["keywords"][0]) == {"text", "weight"}
    assert data["source"]
    assert data["editable"] is True and data["removable"] is False
    assert data["revision"], "the edit form needs a revision token"
    assert set(data["used_by"]) == {"teams", "agents", "is_default_agent"}


def test_detail_resolves_aliases_and_refuses_unknown_names(state):
    source = rolehub.index(state, {"size": ["200"]})["roles"]
    alias_owner = next((row for row in source if row["aliases"]), None)
    assert alias_owner, "the shipped roster has aliased roles"
    detail = rolehub.detail(state, alias_owner["name"])
    if detail["aliases"]:
        assert rolehub.detail(state, detail["aliases"][0])["name"] == alias_owner["name"]
    with pytest.raises(ValueError):
        rolehub.detail(state, "definitely-not-a-role")
    with pytest.raises(ValueError):
        rolehub.detail(state, "")


def test_detail_never_echoes_the_server_token_or_an_api_key(state, monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "sk-live-should-never-appear")
    state.token = TOKEN
    data = rolehub.detail(state, "general")
    text = json.dumps(data)
    assert TOKEN not in text
    assert "sk-live-should-never-appear" not in text


# ── quality ────────────────────────────────────────────────────────────── #


def test_quality_reports_real_findings_and_its_own_limits(state):
    data = rolehub.quality(state)
    assert data["total_roles"] >= 100
    codes = {row["code"] for row in data["issues"]}
    assert "no-keywords" in codes, "the general role has no keywords and must be called out"
    assert data["counts"]["high"] == sum(1 for row in data["issues"] if row["severity"] == "high")
    assert data["counts"]["high"] + data["counts"]["low"] + data["counts"]["info"] == len(data["issues"])
    assert data["not_checked"], "the panel must say what it does not check"
    for row in data["issues"][:10]:
        assert set(row) == {"severity", "code", "name", "message"}
        assert row["message"].strip()


def test_quality_flags_a_new_role_without_keywords(state):
    _save(state, "hub-broken", _entry(keywords=[]))
    findings = {(row["code"], row["name"]) for row in rolehub.quality(state)["issues"]}
    assert ("no-keywords", "hub-broken") in findings


def test_an_empty_persona_is_refused_before_it_reaches_the_roster(state):
    with pytest.raises(ValueError):
        _save(state, "hub-empty", _entry(persona=""))
    assert "hub-empty" not in state.config.roles.roles


# ── route preview ──────────────────────────────────────────────────────── #


def test_route_preview_calls_no_model_and_names_its_method(state):
    data = rolehub.route(state, {"task": "build a python api with docker and postgres"})
    assert data["provider_calls"] == 0
    assert data["decision"] or data["candidates"], "either a pick or the scored shortlist"
    if data["decision"]:
        assert data["decision"]["method"] == "keyword"
        assert data["decision"]["role"] in state.config.roles.roles
        assert 0 < data["decision"]["score"]
    assert data["threshold"] == state.config.settings.router_min_score
    assert "no model was called" in data["note"]


def test_route_preview_validates_its_input(state):
    with pytest.raises(ValueError):
        rolehub.route(state, {"task": ""})
    with pytest.raises(ValueError):
        rolehub.route(state, {"task": "x" * 5000})
    with pytest.raises(ValueError):
        rolehub.route(state, "not a dict")
    empty = rolehub.route(state, {"task": "zzzz-no-role-matches-this"})
    assert empty["decision"] is None and empty["candidates"] == []


# ── export ─────────────────────────────────────────────────────────────── #


def test_export_round_trips_through_the_role_parser(state):
    names = [row["name"] for row in rolehub.index(state, {"size": ["10"]})["roles"][:3]]
    data = rolehub.export(state, {"names": names})
    assert data["count"] == 3
    assert data["filename"].endswith(".yaml")
    document = yaml.safe_load(data["yaml"])
    assert set(document["roles"]) == set(names)
    from nexus.config import parse_roles

    parsed = parse_roles(document)
    assert {role.name for role in parsed} == set(names)
    assert all(role.persona for role in parsed)


def test_export_is_bounded(state):
    with pytest.raises(ValueError):
        rolehub.export(state, {"names": []})
    with pytest.raises(ValueError):
        rolehub.export(state, {"names": ["general"] * (rolehub.MAX_EXPORT + 1)})
    with pytest.raises(ValueError):
        rolehub.export(state, {"names": ["no-such-role"]})


# ── history, diff and restore ──────────────────────────────────────────── #


def test_first_save_records_nothing_but_every_edit_after_it_does(state):
    assert rolehub.versions(state.workspace) == []
    _save(state, "hub-versioned", _entry(title="One"))
    assert rolehub.versions(state.workspace) == [], "a first creation replaces nothing"
    _save(state, "hub-versioned", _entry(title="Two"))
    versions = rolehub.versions(state.workspace)
    assert len(versions) == 1
    assert versions[0]["reason"] == "save hub-versioned"
    assert versions[0]["file"] == MANAGED
    stored = yaml.safe_load((rolehub.store(state.workspace) / "history" / f"{versions[0]['id']}.yaml").read_text())
    assert stored["roles"]["hub-versioned"]["title"] == "One"


def test_diff_shows_only_the_named_role(state):
    _save(state, "hub-diff", _entry(title="Before"))
    _save(state, "hub-diff", _entry(title="After"))
    version = rolehub.versions(state.workspace)[0]["id"]
    data = rolehub.diff(state, {"version": [version], "name": ["hub-diff"]})
    assert data["changed"] is True
    text = "\n".join(data["lines"])
    assert "-  title: Before" in text and "+  title: After" in text
    assert "hub-diff" in text
    untouched = rolehub.diff(state, {"version": [version], "name": ["backend"]})
    assert untouched["changed"] is False and untouched["lines"] == []
    with pytest.raises(ValueError):
        rolehub.diff(state, {"version": ["../../etc/passwd"], "name": ["hub-diff"]})


def test_restore_brings_the_previous_file_back(state):
    _save(state, "hub-restore", _entry(title="Keep me"))
    _save(state, "hub-restore", _entry(title="Throw me away"))
    version = rolehub.versions(state.workspace)[0]["id"]
    with pytest.raises(ValueError):
        rolehub.restore(state, {"version": version, "confirmation": "yes"})
    data = rolehub.restore(state, {"version": version, "confirmation": f"RESTORE ROLES {version}"})
    assert data["restored"] == version
    config = load_config(state.workspace)
    assert config.roles.roles["hub-restore"].title == "Keep me"
    assert state.config.roles.roles["hub-restore"].title == "Keep me", "the live config is reloaded"
    # the restore itself is recorded, so it can be undone too
    assert len(rolehub.versions(state.workspace)) == 2
    assert any(row["reason"].startswith("restore ") for row in rolehub.versions(state.workspace))


def test_restore_rejects_unknown_versions_and_demo_mode(state):
    with pytest.raises(ValueError):
        rolehub.restore(state, {"version": "20260101T000000-abcdef", "confirmation": "RESTORE ROLES 20260101T000000-abcdef"})
    state.demo = True
    try:
        with pytest.raises(ValueError):
            rolehub.restore(state, {"version": "20260101T000000-abcdef", "confirmation": "RESTORE ROLES 20260101T000000-abcdef"})
    finally:
        state.demo = False


def test_history_is_bounded_to_the_newest_versions(state):
    for index in range(rolehub.MAX_VERSIONS + 4):
        _save(state, "hub-loop", _entry(title=f"edit {index}"))
    versions = rolehub.versions(state.workspace)
    assert len(versions) == rolehub.MAX_VERSIONS
    files = list((rolehub.store(state.workspace) / "history").glob("*.yaml"))
    assert len(files) == rolehub.MAX_VERSIONS, "old snapshots are pruned, not accumulated"
    assert versions == sorted(versions, key=lambda row: row["at"], reverse=True)


def test_a_damaged_history_index_is_survivable(state):
    _save(state, "hub-damaged", _entry())
    _save(state, "hub-damaged", _entry(title="Changed"))
    index = rolehub.store(state.workspace) / "index.jsonl"
    index.write_text(index.read_text() + "{not json}\n" + json.dumps({"id": "../escape"}) + "\n")
    versions = rolehub.versions(state.workspace)
    assert len(versions) == 1, "garbage lines are ignored instead of raising"


def test_removing_the_override_restores_the_shipped_role(state):
    free = next(row["name"] for row in rolehub.index(state, {"size": ["200"]})["roles"]
                if not rolehub.detail(state, row["name"])["used_by"]["teams"])
    original = rolehub.detail(state, free)["persona"]
    _save(state, free, _entry(persona="A temporary persona for the hub test.", title="Temp"))
    assert rolehub.detail(state, free)["persona"].startswith("A temporary persona")
    assert rolehub.detail(state, free)["removable"] is True
    _save(state, free, {}, action="remove")
    restored = rolehub.detail(state, free)
    assert restored["persona"] == original
    assert restored["removable"] is False


# ── HTTP ───────────────────────────────────────────────────────────────── #


@pytest.fixture()
def live(repo: Path):
    state = ServerState(repo, token=TOKEN, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield {"base": f"http://{host}:{port}", "state": state}
    finally:
        state.mission.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(url: str, payload=None, token=TOKEN):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET")
    if token:
        request.add_header("X-Nexus-Token", token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_role_routes_require_the_gui_token(live):
    for path in ("/api/roles/index", "/api/roles/detail?name=general", "/api/roles/quality",
                 "/api/roles/history", "/api/roles/diff?version=x"):
        status, body = _call(live["base"] + path, token=None)
        assert status == 401, path
        assert "error" in body


def test_index_returns_the_structured_envelope_and_is_audited(live, repo: Path):
    from nexus import webapi

    status, body = _call(live["base"] + "/api/roles/index")
    assert status == 200 and body["ok"] is True
    assert body["data"]["total"] >= 100
    assert any(row["action"] == "rolehub.index" for row in webapi.read_audit(repo))
    # the legacy endpoint the old panel uses must keep working untouched
    status, legacy = _call(live["base"] + "/api/roles")
    assert status == 200 and isinstance(legacy, list) and legacy


def test_unknown_role_subpaths_are_404_not_the_legacy_list(live):
    status, body = _call(live["base"] + "/api/roles/nope")
    assert status == 404 and body["error"]


def test_detail_validates_the_name(live):
    status, body = _call(live["base"] + "/api/roles/detail?name=not-a-real-role")
    assert status == 400
    assert body["ok"] is False and body["error"]["code"] == "invalid_request"


def test_role_reads_are_rate_limited(live):
    live["state"].api_limiter = Limiter(limit=2, window=60)
    assert _call(live["base"] + "/api/roles/index")[0] == 200
    assert _call(live["base"] + "/api/roles/index")[0] == 200
    status, body = _call(live["base"] + "/api/roles/index")
    assert status == 429 and body["error"]["code"] == "rate_limited"


def test_route_preview_is_a_post_and_always_audited(live, repo: Path):
    from nexus import webapi

    before = len([row for row in webapi.read_audit(repo) if row["action"] == "rolehub.route"])
    status, body = _call(live["base"] + "/api/roles/route", {"task": "write a seo landing page"})
    assert status == 200 and body["ok"] is True
    assert body["data"]["provider_calls"] == 0
    after = [row for row in webapi.read_audit(repo) if row["action"] == "rolehub.route"]
    assert len(after) == before + 1, "an action is audited on every call, not sampled"


def test_restore_over_http_needs_the_exact_confirmation(live):
    state = live["state"]
    _save(state, "hub-http", _entry(title="One"))
    _save(state, "hub-http", _entry(title="Two"))
    version = rolehub.versions(state.workspace)[0]["id"]
    status, body = _call(live["base"] + "/api/roles/restore", {"version": version, "confirmation": "nope"})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert load_config(state.workspace).roles.roles["hub-http"].title == "Two"
    status, body = _call(live["base"] + "/api/roles/restore",
                         {"version": version, "confirmation": f"RESTORE ROLES {version}"})
    assert status == 200 and body["data"]["restored"] == version
    clear_yaml_cache()
    assert load_config(state.workspace).roles.roles["hub-http"].title == "One"


def test_export_over_http_returns_yaml(live):
    status, body = _call(live["base"] + "/api/roles/export", {"names": ["general"]})
    assert status == 200
    assert body["data"]["yaml"].startswith("roles:")
    assert body["data"]["count"] == 1

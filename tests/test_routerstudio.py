"""Router Studio backend: policy storage, reviewed rules, dry runs, lab, accuracy.

Every test uses a throwaway workspace (``repo``) and an isolated ``NEXUS_HOME``
from ``conftest.py``, so the developer's real sessions and routing policy are
never read or written.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import nexus.routerstudio as studio
from nexus.config import load_config
from nexus.router import Router
from nexus.server import ServerState, make_handler
from nexus.sessions import SessionStore
from nexus.webapi import Limiter

TOKEN = "router-studio-test-token"
PHRASE = "quantum-zebra"


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        created.mission.close()


def _role(state, preferred: str = "backend") -> str:
    names = sorted(state.config.roles.roles)
    return preferred if preferred in names else names[0]


def _rule(state, **overrides):
    base = {"phrase": PHRASE, "role": _role(state), "mode": "contains", "weight": 4.0,
            "priority": 6, "enabled": True, "note": "studio test"}
    base.update(overrides)
    return base


def _save_rule(state, **overrides):
    prepared = studio.prepare_rule(state, {"rule": _rule(state, **overrides)})
    return studio.rule_action(state, {"action": "save", "rule": prepared["rule"],
                                      "revision": prepared["revision"],
                                      "confirmation": prepared["confirmation"]})


def _enable(state, enabled: bool = True):
    policy = studio.load_policy(state.workspace)
    token = "ENABLE RULES" if enabled else "DISABLE RULES"
    return studio.set_enabled(state, {"enabled": enabled, "revision": policy.revision, "confirmation": token})


def _seed_session(store: SessionStore, workspace: Path, *, agent: str, role: str, ok: bool,
                  duration: float, when: float | None = None) -> None:
    import time

    store.stats()  # creates the schema
    with store._lock, store._connect() as conn:
        conn.execute(
            """INSERT INTO sessions (id, created, workspace, role, agent, task, summary, transcript,
            turns, tool_calls, duration_s, ok) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"s-{time.time_ns()}", when or time.time(), str(Path(workspace).resolve()), role, agent,
             "seeded task", "seeded", "", 2, 3, duration, int(ok)),
        )
        conn.commit()


# ── policy storage ─────────────────────────────────────────────────────── #


def test_policy_defaults_to_off_and_reads_without_a_file(state):
    policy = studio.load_policy(state.workspace)
    assert policy.enabled is False and policy.rules == ()
    assert policy.weights == studio.DEFAULT_WEIGHTS
    assert policy.revision and policy.error == ""
    assert not studio.policy_path(state.workspace).exists()
    assert studio.versions(state.workspace) == []


def test_a_damaged_policy_is_ignored_instead_of_breaking_routing(state):
    path = studio.policy_path(state.workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    policy = studio.load_policy(state.workspace)
    assert policy.enabled is False and policy.rules == ()
    assert policy.error.startswith("Routing policy ignored")
    path.write_text(json.dumps({"schema": 99, "enabled": True}), encoding="utf-8")
    assert studio.load_policy(state.workspace).error.startswith("Routing policy ignored")
    # the built-in router still works with the same damaged file in place
    decision = Router(state.config, providers=None).keyword_route("build a python api")
    assert decision is None or decision.method == "keyword"


def test_saving_a_rule_needs_the_typed_confirmation_and_the_current_revision(state):
    prepared = studio.prepare_rule(state, {"rule": _rule(state)})
    assert prepared["confirmation"] == f"SAVE RULE {prepared['id']}"
    assert prepared["replaces"] is None
    with pytest.raises(ValueError, match="SAVE RULE"):
        studio.rule_action(state, {"action": "save", "rule": prepared["rule"], "revision": prepared["revision"],
                                   "confirmation": "yes"})
    assert not studio.policy_path(state.workspace).exists(), "a refused save writes nothing"
    with pytest.raises(ValueError, match="changed since"):
        studio.rule_action(state, {"action": "save", "rule": prepared["rule"], "revision": "stale",
                                   "confirmation": prepared["confirmation"]})


def test_saving_a_rule_writes_owner_only_state_and_bumps_the_revision(state):
    before = studio.load_policy(state.workspace).revision
    body = _save_rule(state)
    path = studio.policy_path(state.workspace)
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    stored = json.loads(path.read_text())
    assert stored["rules"][0]["phrase"] == PHRASE
    assert stored["rules"][0]["role"] == _role(state)
    assert body["saved"] == "created"
    assert body["revision"] == studio.load_policy(state.workspace).revision != before
    assert studio.versions(state.workspace) == [], "the first save replaces nothing"


def test_the_second_change_records_the_previous_policy(state):
    _save_rule(state)
    payload = studio.save_weights(state, {"weights": {"cost": 10, "speed": 20, "quality": 70},
                                          "revision": studio.load_policy(state.workspace).revision,
                                          "confirmation": "SAVE WEIGHTS"})
    assert payload["weights"] == {"cost": 10, "speed": 20, "quality": 70}
    versions = studio.versions(state.workspace)
    assert len(versions) == 1
    assert versions[0]["rules"] == 1 and versions[0]["enabled"] is False
    assert versions[0]["reason"] == "save weights"


# ── rule validation ────────────────────────────────────────────────────── #


def test_rules_are_validated_against_the_roster(state):
    with pytest.raises(ValueError, match="Unknown role"):
        studio.prepare_rule(state, {"rule": _rule(state, role="not-a-real-role")})
    with pytest.raises(ValueError, match="1-160"):
        studio.prepare_rule(state, {"rule": _rule(state, phrase="")})
    with pytest.raises(ValueError, match="phrase"):
        studio.prepare_rule(state, {"rule": _rule(state, phrase="x" * 161)})
    with pytest.raises(ValueError):
        studio.prepare_rule(state, {"rule": _rule(state, weight=99)})
    with pytest.raises(ValueError):
        studio.prepare_rule(state, {"rule": _rule(state, priority=0)})
    with pytest.raises(ValueError):
        studio.prepare_rule(state, {"rule": _rule(state, mode="regex")})
    with pytest.raises(ValueError):
        studio.prepare_rule(state, {"rule": "not a dict"})


def test_repeating_a_rule_is_an_edit_and_a_near_duplicate_is_refused(state):
    saved = _save_rule(state)["rule"]
    again = studio.prepare_rule(state, {"rule": _rule(state, weight=6.0)})
    assert again["id"] == saved["id"] and again["replaces"] == saved["id"]
    with pytest.raises(ValueError, match="already exists"):
        studio.prepare_rule(state, {"rule": _rule(state, mode="words")})


def test_the_rule_budget_is_enforced(state, monkeypatch):
    monkeypatch.setattr(studio, "MAX_RULES", 2)
    _save_rule(state, phrase="alpha-rule")
    _save_rule(state, phrase="beta-rule")
    with pytest.raises(ValueError, match="At most 2"):
        studio.prepare_rule(state, {"rule": _rule(state, phrase="gamma-rule")})


def test_a_rule_can_be_disabled_enabled_and_removed(state):
    saved = _save_rule(state)["rule"]
    revision = studio.load_policy(state.workspace).revision
    with pytest.raises(ValueError, match="DISABLE RULE"):
        studio.rule_action(state, {"action": "disable", "id": saved["id"], "revision": revision,
                                   "confirmation": "no"})
    body = studio.rule_action(state, {"action": "disable", "id": saved["id"], "revision": revision,
                                      "confirmation": f"DISABLE RULE {saved['id']}"})
    assert body["policy"]["rules"][0]["enabled"] is False
    revision = body["revision"]
    body = studio.rule_action(state, {"action": "enable", "id": saved["id"], "revision": revision,
                                      "confirmation": f"ENABLE RULE {saved['id']}"})
    assert body["policy"]["rules"][0]["enabled"] is True
    revision = body["revision"]
    body = studio.rule_action(state, {"action": "remove", "id": saved["id"], "revision": revision,
                                      "confirmation": f"REMOVE RULE {saved['id']}"})
    assert body["policy"]["rules"] == []


def test_demo_mode_refuses_every_write(state):
    state.demo = True
    try:
        with pytest.raises(ValueError, match="demo mode"):
            _save_rule(state)
        with pytest.raises(ValueError, match="demo mode"):
            studio.save_weights(state, {"weights": {"cost": 1, "speed": 1, "quality": 1},
                                        "revision": studio.load_policy(state.workspace).revision,
                                        "confirmation": "SAVE WEIGHTS"})
    finally:
        state.demo = False
    assert not studio.policy_path(state.workspace).exists()


# ── the live router reads the rule book ────────────────────────────────── #


def test_enabled_rules_reach_the_router_and_a_disabled_policy_does_not(state):
    router = Router(state.config, providers=None)
    baseline = router.keyword_route("please run the quantum-zebra migration")
    assert baseline is None or baseline.role.name != _role(state) or PHRASE not in " ".join(baseline.hits)
    _save_rule(state)
    assert router.keyword_route(PHRASE) is None, "the master switch is off"
    _enable(state, True)
    decision = router.keyword_route(PHRASE)
    assert decision is not None
    assert decision.role.name == _role(state)
    assert decision.score >= 4.0
    assert f"rule:{_rule_id(state)}" in decision.hits
    _enable(state, False)
    assert router.keyword_route(PHRASE) is None


def _rule_id(state) -> str:
    return studio.load_policy(state.workspace).rules[0].id


def test_rules_can_add_a_role_with_no_keyword_hit(state):
    _save_rule(state, phrase="wombat-orchestration", weight=3.0)
    _enable(state, True)
    decision = Router(state.config, providers=None).keyword_route("wombat-orchestration")
    assert decision is not None and decision.role.name == _role(state)
    assert decision.hits == [f"rule:{_rule_id(state)}"]


def test_rule_priority_breaks_an_exact_tie(state):
    roles = sorted(state.config.roles.roles)
    first, second = roles[0], roles[1]
    _save_rule(state, phrase="tie-breaker", role=first, priority=2)
    _save_rule(state, phrase="tie-breaker", role=second, priority=9)
    _enable(state, True)
    decision = Router(state.config, providers=None).keyword_route("tie-breaker")
    assert decision is not None and decision.role.name == second


# ── dry runs ───────────────────────────────────────────────────────────── #


def test_simulate_is_offline_and_matches_the_router(state):
    body = studio.simulate(state, {"task": "build a python api with docker and postgres"})
    assert body["provider_calls"] == 0
    assert body["policy_enabled"] is False
    assert body["rules_matched"] == []
    assert body["threshold"] == float(state.config.settings.router_min_score)
    assert body["elapsed_ms"] >= 0
    if body["decision"]:
        assert body["decision"]["method"] == "keyword"
        live = Router(state.config, providers=None).keyword_route("build a python api with docker and postgres")
        assert live is not None and live.role.name == body["decision"]["role"]
    assert "no model was called" in body["note"]


def test_simulate_previews_an_unsaved_draft_without_persisting(state):
    task = "route this through the phantom pipeline"
    body = studio.simulate(state, {"task": task, "draft": _rule(state, phrase="phantom pipeline", weight=6.0)})
    assert body["decision"] is not None
    assert body["decision"]["role"] == _role(state)
    assert body["rules_matched"] and body["rules_matched"][0]["phrase"] == "phantom pipeline"
    assert not studio.policy_path(state.workspace).exists(), "a preview never writes the policy"
    assert body["policy_enabled"] is False


def test_simulate_explains_a_deferral(state):
    body = studio.simulate(state, {"task": "zzzqqq blorptastic nothing"})
    assert body["decision"] is None
    assert body["deferred"]["stage"] == ("classifier" if state.config.settings.router_llm_fallback else "default")
    assert "below the" in body["deferred"]["reason"] or "No keyword" in body["deferred"]["reason"]


def test_simulate_validates_its_input(state):
    with pytest.raises(ValueError):
        studio.simulate(state, {"task": ""})
    with pytest.raises(ValueError):
        studio.simulate(state, {"task": "x" * 5000})
    with pytest.raises(ValueError):
        studio.simulate(state, "not a dict")


# ── weights and the model ranking ──────────────────────────────────────── #


def test_weights_need_the_revision_and_the_typed_confirmation(state):
    revision = studio.load_policy(state.workspace).revision
    with pytest.raises(ValueError, match="SAVE WEIGHTS"):
        studio.save_weights(state, {"weights": {"cost": 1, "speed": 1, "quality": 1},
                                    "revision": revision, "confirmation": "yes"})
    with pytest.raises(ValueError, match="changed since"):
        studio.save_weights(state, {"weights": {"cost": 1, "speed": 1, "quality": 1},
                                    "revision": "stale", "confirmation": "SAVE WEIGHTS"})
    with pytest.raises(ValueError, match="above zero"):
        studio.save_weights(state, {"weights": {"cost": 0, "speed": 0, "quality": 0},
                                    "revision": revision, "confirmation": "SAVE WEIGHTS"})
    with pytest.raises(ValueError):
        studio.save_weights(state, {"weights": {"cost": 101, "speed": 1, "quality": 1},
                                    "revision": revision, "confirmation": "SAVE WEIGHTS"})
    assert studio.load_policy(state.workspace).weights == studio.DEFAULT_WEIGHTS


def test_model_ranking_invents_nothing(state):
    data = studio.model_ranking(state)
    assert data["recommendation"] is None
    assert data["pricing_configured"] == 0
    assert data["agents"] and all(row["score"] is None and row["coverage"] == 0 for row in data["agents"])
    assert all(row["price"] is None and row["avg_duration_s"] is None for row in data["agents"])
    assert "not model latency" in data["basis"]


def test_model_ranking_uses_prices_and_recorded_sessions(state):
    agents = state.config.agents
    cheap, pricey = sorted(agents)[0], sorted(agents)[1]
    state.workbench.save_settings({"pricing": {
        agents[cheap].model: {"source": "operator estimate", "input_per_million": 1.0, "output_per_million": 1.0},
        agents[pricey].model: {"source": "operator estimate", "input_per_million": 10.0, "output_per_million": 10.0},
    }})
    store = SessionStore()
    for _ in range(4):
        _seed_session(store, state.workspace, agent=cheap, role=_role(state), ok=True, duration=5.0)
        _seed_session(store, state.workspace, agent=pricey, role=_role(state), ok=False, duration=50.0)
    data = studio.model_ranking(state)
    rows = {row["agent"]: row for row in data["agents"]}
    assert rows[cheap]["coverage"] == 3 and rows[pricey]["coverage"] == 3
    assert rows[cheap]["score"] == 100.0 and rows[pricey]["score"] == 0.0
    assert data["recommendation"]["agent"] == cheap
    # weights only reorder this table: make cost dominate and the cheap agent stays first...
    studio.save_weights(state, {"weights": {"cost": 100, "speed": 0, "quality": 0},
                                "revision": studio.load_policy(state.workspace).revision,
                                "confirmation": "SAVE WEIGHTS"})
    cost_only = {row["agent"]: row["score"] for row in studio.model_ranking(state)["agents"]}
    assert cost_only[cheap] == 100.0 and cost_only[pricey] == 0.0
    # ...and make reported quality dominate: still the cheap (also reliable) agent here
    studio.save_weights(state, {"weights": {"cost": 0, "speed": 0, "quality": 100},
                                "revision": studio.load_policy(state.workspace).revision,
                                "confirmation": "SAVE WEIGHTS"})
    quality_only = {row["agent"]: row["score"] for row in studio.model_ranking(state)["agents"]}
    assert quality_only[cheap] == 100.0 and quality_only[pricey] == 0.0


def test_model_ranking_follows_the_weights_when_measurements_disagree(state):
    agents = state.config.agents
    cheap, pricey = sorted(agents)[0], sorted(agents)[1]
    state.workbench.save_settings({"pricing": {
        agents[cheap].model: {"source": "operator estimate", "input_per_million": 1.0, "output_per_million": 0.0},
        agents[pricey].model: {"source": "operator estimate", "input_per_million": 9.0, "output_per_million": 0.0},
    }})
    store = SessionStore()
    for _ in range(3):
        _seed_session(store, state.workspace, agent=cheap, role=_role(state), ok=False, duration=40.0)
        _seed_session(store, state.workspace, agent=pricey, role=_role(state), ok=True, duration=6.0)
    revision = studio.load_policy(state.workspace).revision
    studio.save_weights(state, {"weights": {"cost": 100, "speed": 0, "quality": 0},
                                "revision": revision, "confirmation": "SAVE WEIGHTS"})
    assert studio.model_ranking(state)["recommendation"]["agent"] == cheap
    studio.save_weights(state, {"weights": {"cost": 0, "speed": 0, "quality": 100},
                                "revision": studio.load_policy(state.workspace).revision,
                                "confirmation": "SAVE WEIGHTS"})
    assert studio.model_ranking(state)["recommendation"]["agent"] == pricey


# ── fallback ladders ───────────────────────────────────────────────────── #


def test_chains_report_unknown_targets_and_closed_ladders(state):
    cfg = state.config
    healthy = cfg.agents["gemini"]
    cfg.agents = dict(cfg.agents)
    cfg.agents["gemini"] = replace(healthy, fallback="ghost")
    cfg.agents["ghost"] = replace(healthy, name="ghost", fallback="gemini")
    data = studio.chains(state)
    rows = {row["agent"]: row for row in data["agents"]}
    gemini = rows["gemini"]
    assert [node["name"] for node in gemini["nodes"]] == ["gemini", "ghost"]
    assert any(issue["code"] == "loop" for issue in gemini["issues"])
    cfg.agents["gemini"] = replace(healthy, fallback="does-not-exist")
    data = studio.chains(state)
    gemini = {row["agent"]: row for row in data["agents"]}["gemini"]
    assert any(issue["code"] == "unknown" for issue in gemini["issues"])
    assert data["summary"]["broken"] >= 1


# ── accuracy and the lab ───────────────────────────────────────────────── #


def test_accuracy_reports_feedback_and_its_own_blind_spots(state):
    store = SessionStore()
    store.record_routing_feedback("build api", [_role(state)], True, workspace=state.workspace)
    store.record_routing_feedback("build api", [_role(state)], False, workspace=state.workspace)
    _seed_session(store, state.workspace, agent=sorted(state.config.agents)[0], role=_role(state),
                  ok=True, duration=12.0)
    data = studio.accuracy(state)
    assert data["feedback"]["observations"] == 2
    assert data["feedback"]["approved"] == 1
    assert data["feedback"]["approval_rate"] == pytest.approx(0.5)
    assert data["totals"]["runs"] == 1
    assert data["roles"][0]["role"] == _role(state)
    assert any(item["code"] == "classifier" for item in data["not_checked"])
    assert data["checked"] and all(set(item) == {"code", "text"} for item in data["checked"] + data["not_checked"])


def test_lab_scores_expected_roles_and_records_the_run(state):
    task = "build a python api with docker and postgres"
    decision = studio.simulate(state, {"task": task})["decision"]
    assert decision is not None, "the curated roster must route a python api task"
    body = studio.run_lab(state, {"cases": [{"task": task, "expected": decision["role"]},
                                            {"task": "qqqxxzz blorptastic", "expected": decision["role"]}]})
    run = body["run"]
    assert run["provider_calls"] == 0
    assert run["with_expected"] == 2 and run["correct"] == 1 and run["accuracy"] == 0.5
    assert run["cases"][0]["pass"] is True and run["cases"][1]["pass"] is False
    history = studio.lab(state)["runs"]
    assert history and history[0]["id"] == run["id"]
    assert studio.lab(state)["runs"][0]["policy_revision"] == studio.load_policy(state.workspace).revision


def test_lab_is_bounded(state):
    with pytest.raises(ValueError, match="1-25"):
        studio.run_lab(state, {"cases": []})
    with pytest.raises(ValueError, match="1-25"):
        studio.run_lab(state, {"cases": [{"task": "x"}] * 26})
    with pytest.raises(ValueError):
        studio.run_lab(state, {"cases": [{"task": "x" * 501}]})
    with pytest.raises(ValueError, match="Unknown role"):
        studio.run_lab(state, {"cases": [{"task": "x", "expected": "not-a-role"}]})
    assert studio.lab(state)["runs"] == []


# ── history and restore ────────────────────────────────────────────────── #


def test_restore_round_trips_the_policy(state):
    _save_rule(state, phrase="first-rule")
    studio.save_weights(state, {"weights": {"cost": 5, "speed": 5, "quality": 90},
                                "revision": studio.load_policy(state.workspace).revision,
                                "confirmation": "SAVE WEIGHTS"})
    _save_rule(state, phrase="second-rule")
    assert len(studio.load_policy(state.workspace).rules) == 2
    version_payload = {"revision": studio.load_policy(state.workspace).revision}
    version = studio.versions(state.workspace)[0]["id"]
    with pytest.raises(ValueError, match="RESTORE ROUTER"):
        studio.restore(state, {"version": version, "confirmation": "nope",
                               "revision": version_payload["revision"]})
    restored = studio.restore(state, {"version": version,
                                      "confirmation": f"RESTORE ROUTER {version}",
                                      "revision": studio.load_policy(state.workspace).revision})
    assert restored["restored"] == version
    assert [rule.phrase for rule in studio.load_policy(state.workspace).rules] == ["first-rule"]
    assert studio.load_policy(state.workspace).weights["quality"] == 90
    # the restore itself is recorded, so it can be undone too
    assert any(row["reason"].startswith("restore ") for row in studio.versions(state.workspace))


def test_restore_rejects_unknown_versions(state):
    with pytest.raises(ValueError):
        studio.restore(state, {"version": "../../etc/passwd", "confirmation": "RESTORE ROUTER x"})
    with pytest.raises(ValueError, match="No such recorded"):
        studio.restore(state, {"version": "20260101T000000-deadbeef",
                               "confirmation": "RESTORE ROUTER 20260101T000000-deadbeef"})


# ── secrets ────────────────────────────────────────────────────────────── #


def test_reads_never_echo_the_server_token(state):
    _save_rule(state, note=f"accidental paste {TOKEN}")
    for payload in (studio.overview(state), studio.rules(state), studio.accuracy(state),
                    studio.lab(state), studio.model_ranking(state), studio.chains(state)):
        assert TOKEN not in json.dumps(payload), "the GUI token must never reach a response"


# ── HTTP ───────────────────────────────────────────────────────────────── #


@pytest.fixture()
def live(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(created))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield {"base": f"http://{host}:{port}", "state": created}
    finally:
        created.mission.close()
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
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_router_routes_require_the_gui_token(live):
    for path in ("/api/router/overview", "/api/router/rules", "/api/router/chains", "/api/router/models",
                 "/api/router/accuracy", "/api/router/history", "/api/router/lab"):
        status, body = _call(live["base"] + path, token=None)
        assert status == 401, path
        assert "error" in body


def test_router_reads_use_the_structured_envelope_and_are_audited(live, repo: Path):
    from nexus import webapi

    status, body = _call(live["base"] + "/api/router/overview")
    assert status == 200 and body["ok"] is True
    assert body["data"]["roster"]["roles"] >= 100
    assert any(row["action"] == "routerstudio.overview" for row in webapi.read_audit(repo))


def test_unknown_router_subpaths_are_404(live):
    status, body = _call(live["base"] + "/api/router/nope")
    assert status == 404 and body["error"]


def test_router_reads_are_rate_limited(live):
    live["state"].api_limiter = Limiter(limit=2, window=60)
    assert _call(live["base"] + "/api/router/rules")[0] == 200
    assert _call(live["base"] + "/api/router/rules")[0] == 200
    status, body = _call(live["base"] + "/api/router/rules")
    assert status == 429 and body["error"]["code"] == "rate_limited"


def test_simulate_is_a_post_and_never_runs_a_task(live):
    state = live["state"]
    before = len(state.sessions)
    status, body = _call(live["base"] + "/api/router/simulate", {"task": "build a python api"})
    assert status == 200 and body["data"]["provider_calls"] == 0
    assert len(state.sessions) == before, "a dry run must not create a session"
    status, _ = _call(live["base"] + "/api/router/simulate")
    assert status == 404, "the preview is POST-only"


def test_saving_a_rule_over_http_needs_the_exact_confirmation(live, repo: Path):
    state = live["state"]
    prepared = studio.prepare_rule(state, {"rule": _rule(state)})
    status, body = _call(live["base"] + "/api/router/rule",
                         {"action": "save", "rule": prepared["rule"],
                          "revision": prepared["revision"], "confirmation": "nope"})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert not studio.policy_path(repo).exists()
    status, body = _call(live["base"] + "/api/router/rule",
                         {"action": "save", "rule": prepared["rule"],
                          "revision": prepared["revision"], "confirmation": prepared["confirmation"]})
    assert status == 200 and body["data"]["saved"] == "created"
    status, body = _call(live["base"] + "/api/router/lab",
                         {"cases": [{"task": "quantum-zebra", "expected": _role(state)}]})
    assert status == 200 and body["data"]["run"]["total"] == 1


def test_weights_over_http_round_trip(live, repo: Path):
    revision = studio.load_policy(repo).revision
    status, body = _call(live["base"] + "/api/router/weights",
                         {"weights": {"cost": 1, "speed": 1, "quality": 1}, "revision": revision,
                          "confirmation": "SAVE WEIGHTS"})
    assert status == 200 and body["data"]["weights"] == {"cost": 1, "speed": 1, "quality": 1}
    assert json.loads(studio.policy_path(repo).read_text())["weights"]["quality"] == 1


def test_demo_mode_is_refused_over_http(live, repo: Path):
    state = live["state"]
    state.demo = True
    try:
        status, body = _call(live["base"] + "/api/router/enable",
                             {"enabled": True, "revision": studio.load_policy(repo).revision,
                              "confirmation": "ENABLE RULES"})
        assert status == 400
        assert "demo mode" in body["error"]["message"]
    finally:
        state.demo = False
    assert not studio.policy_path(repo).exists()
    assert load_config(repo).roles is not None

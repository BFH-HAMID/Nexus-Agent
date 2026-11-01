"""The World desk: what is cached, how old it is, and the one way to refresh it.

Every test drives the real ``World`` object the GUI uses, with a scripted HTTP
transport, so no publisher is contacted and nothing is invented. The desk's
central promise — that *reading* the page touches no network — is asserted by
counting transport calls, not by trusting the docstring.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

import nexus.worlddesk as wd
from nexus.hud.world.core import World
from nexus.server import ServerState, make_handler

TOKEN = "world-desk-test-token"
FEED = b"""<rss version="2.0"><channel><title>Test</title>
<item><title>Dhaka headline</title><link>https://example.org/a</link>
<pubDate>Thu, 29 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>"""

QUAKE = {"type": "FeatureCollection", "features": [
    {"id": "q1", "geometry": {"coordinates": [88.6, 25.6, 8]},
     "properties": {"time": time.time() * 1000 - 3600_000, "mag": 4.2, "place": "Dinajpur",
                    "url": "https://example.org/quake"}},
]}


@pytest.fixture()
def state(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    try:
        yield created
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()


def _world(repo: Path, *, handler=None, clock=None, weather_key=None) -> World:
    calls: list[str] = []

    def upstream(request):
        calls.append(str(request.url))
        if handler is not None:
            return handler(request)
        if "earthquake" in str(request.url):
            return httpx.Response(200, json=QUAKE)
        return httpx.Response(200, content=FEED)

    box = World(repo, cache_dir=repo / ".nexus" / "worldcache",
                transport=httpx.MockTransport(upstream), sleep=lambda _: None,
                weather_key=weather_key or (lambda: ""))
    box.calls = calls  # type: ignore[attr-defined]
    return box


def _configured(repo: Path) -> None:
    (repo / "widget.yaml").write_text(yaml.safe_dump({"sources": ["bbc", "prothom-alo"]}), encoding="utf-8")


# ── reading the desk never touches the network ─────────────────────────── #


def test_opening_the_desk_makes_no_request(repo: Path, state):
    _configured(repo)
    box = _world(repo)
    state.gui._world = box
    body = wd.overview(state)
    assert box.calls == [], "reading the desk must never contact a publisher"
    assert body["totals"] == {"feeds": 5, "fresh": 0, "stale": 0, "missing": 5, "headlines": 0}
    assert body["enabled"] is True
    assert [row["group"] for row in body["sources"]] == ["news", "news", "crypto", "quakes", "weather"]


def test_every_configured_feed_is_listed_with_its_own_ttl(repo: Path, state):
    _configured(repo)
    box = _world(repo)
    state.gui._world = box
    body = wd.overview(state)
    news = [row for row in body["sources"] if row["group"] == "news"]
    assert [row["name"] for row in news] == ["BBC News · World", "Prothom Alo"]
    assert {row["ttl_s"] for row in news} == {600}
    assert {row["status"] for row in news} == {"missing"}
    assert news[0]["terms"].startswith("https://")
    assert body["refresh"] == {"news": 600, "crypto": 120, "weather": 900, "quakes": 300}


def test_freshness_is_derived_from_the_stored_timestamp(repo: Path, state):
    _configured(repo)
    now = [1_000_000.0]
    box = _world(repo, clock=lambda: now[0])
    box.cache.clock = lambda: now[0]
    box.cache.rows["news:bbc"] = {"at": now[0] - 30, "data": [{"title": "a"}]}
    box.cache.rows["quakes"] = {"at": now[0] - 400, "data": []}
    state.gui._world = box
    body = wd.overview(state)
    by_key = {row["cache_key"]: row for row in body["sources"]}
    fresh = by_key["news:bbc"]
    assert fresh["status"] == "fresh" and fresh["fresh"] is True
    assert fresh["age_s"] == 30 and fresh["due_in_s"] == 570 and fresh["items"] == 1
    stale = by_key["quakes"]
    assert stale["status"] == "stale" and stale["fresh"] is False and stale["due_in_s"] == 0
    assert body["groups"]["news"]["fresh"] == 1, "the fresh feed is counted as fresh"
    assert body["groups"]["quakes"]["stale"] == 1, "an expired cache is counted as stale, never refreshed"
    assert by_key["crypto"]["status"] == "missing" and by_key["crypto"]["at"] is None


def test_an_empty_feed_is_not_reported_as_zero_news(repo: Path, state):
    _configured(repo)
    box = _world(repo)
    state.gui._world = box
    row = wd.overview(state)["sources"][0]
    assert row["status"] == "missing" and row["items"] == 0 and row["present"] is False
    assert row["at"] is None and row["age_s"] is None


def test_a_disabled_world_says_so_instead_of_looking_empty(repo: Path, state):
    (repo / "widget.yaml").write_text(yaml.safe_dump({"enabled": False}), encoding="utf-8")
    state.gui._world = _world(repo)
    body = wd.overview(state)
    assert body["enabled"] is False
    with pytest.raises(ValueError, match="disabled"):
        wd.refresh(state, {"confirmation": wd.REFRESH_TOKEN, "groups": ["news"]})


# ── the one write ──────────────────────────────────────────────────────── #


def test_refresh_needs_the_exact_token(repo: Path, state):
    _configured(repo)
    box = _world(repo)
    state.gui._world = box
    with pytest.raises(ValueError, match="Type REFRESH PUBLIC SOURCES"):
        wd.refresh(state, {"groups": ["news"]})
    with pytest.raises(ValueError, match="Type REFRESH PUBLIC SOURCES"):
        wd.refresh(state, {"confirmation": "refresh", "groups": ["news"]})
    assert box.calls == []


def test_refresh_rejects_unknown_or_empty_groups(repo: Path, state):
    _configured(repo)
    box = _world(repo)
    state.gui._world = box
    for groups in ([], ["weather", "magic"], "news", [f"g{index}" for index in range(9)]):
        with pytest.raises(ValueError):
            wd.refresh(state, {"confirmation": wd.REFRESH_TOKEN, "groups": groups})
    assert box.calls == []


def test_refresh_reports_each_group_and_updates_the_desk(repo: Path, state):
    _configured(repo)
    box = _world(repo)
    state.gui._world = box
    result = wd.refresh(state, {"confirmation": wd.REFRESH_TOKEN, "groups": ["news", "quakes"]})
    assert sorted(result["groups"]) == ["news", "quakes"]
    assert result["results"]["news"]["items"] == 1, "the same story from two feeds is deduplicated by URL"
    assert result["results"]["news"]["offline_sources"] == 0
    assert result["results"]["quakes"]["items"] == 1
    assert result["overview"]["groups"]["news"]["fresh"] == 2
    assert result["overview"]["groups"]["quakes"]["fresh"] == 1
    assert len(box.calls) == 3, "two feeds and one quake request, nothing else"
    assert all(url.startswith("https://") for url in box.calls)


def test_a_failing_source_keeps_the_cache_and_never_raises(repo: Path, state):
    _configured(repo)
    box = _world(repo, handler=lambda request: httpx.Response(503))
    state.gui._world = box
    result = wd.refresh(state, {"confirmation": wd.REFRESH_TOKEN, "groups": ["news"]})
    row = result["results"]["news"]
    assert row["status"] == "offline" and row["offline_sources"] == 2
    assert result["overview"]["groups"]["news"]["missing"] == 2


def test_weather_is_skipped_when_no_credential_is_configured(repo: Path, state):
    _configured(repo)
    box = _world(repo, weather_key=lambda: "")
    state.gui._world = box
    result = wd.refresh(state, {"confirmation": wd.REFRESH_TOKEN, "groups": ["weather"]})
    assert result["results"]["weather"]["status"] == "configuration-required"
    assert box.calls == [], "no request is made without a key"

    keyed = _world(repo, handler=lambda request: httpx.Response(200, json={
        "current": {"temp_c": 30}, "forecast": {"forecastday": []}}), weather_key=lambda: "k")
    state.gui._world = keyed
    assert len(keyed.calls) == 0
    wd.refresh(state, {"confirmation": wd.REFRESH_TOKEN, "groups": ["weather"]})
    assert len(keyed.calls) == 1, "the key is sent to the provider, not to the page"


def test_the_audit_store_failing_does_not_undo_the_fetch(repo: Path, state, monkeypatch):
    _configured(repo)
    box = _world(repo)
    state.gui._world = box

    def broken(*_args, **_kwargs):
        raise OSError("audit volume full")

    monkeypatch.setattr("nexus.securitycenter.audit_action", broken)
    result = wd.refresh(state, {"confirmation": wd.REFRESH_TOKEN, "groups": ["quakes"]})
    assert result["results"]["quakes"]["items"] == 1
    wd.audit_read(state, "overview", "client")  # also must not raise


def test_overview_survives_a_broken_credential_lookup(repo: Path, state):
    _configured(repo)
    def explode():
        raise OSError("vault unavailable")
    box = _world(repo, weather_key=explode)
    state.gui._world = box
    body = wd.overview(state)
    assert body["key_present"] is False
    assert body["provider_label"] == "WeatherAPI.com"


# ── HTTP surface ───────────────────────────────────────────────────────── #


@pytest.fixture()
def live(repo: Path):
    created = ServerState(repo, token=TOKEN, quiet=True)
    _configured(repo)
    created.gui._world = _world(repo)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(created))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield {"base": f"http://{host}:{port}", "state": created, "repo": repo}
    finally:
        if getattr(created, "workflow_scheduler", None):
            created.workflow_scheduler.stop()
        created.mission.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call_http(url: str, payload=None, token=TOKEN):
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


def test_the_world_endpoints_need_the_gui_token(live):
    status, _ = _call_http(live["base"] + "/api/world/overview", token=None)
    assert status == 401
    status, _ = _call_http(live["base"] + "/api/world/refresh", {"confirmation": wd.REFRESH_TOKEN}, token=None)
    assert status == 401


def test_the_overview_route_returns_the_desk_shape(live):
    status, body = _call_http(live["base"] + "/api/world/overview")
    assert status == 200
    data = body["data"]
    assert set(data) >= {"schema", "observed_at", "enabled", "sources", "groups", "totals", "tokens", "limits", "notes"}
    assert data["tokens"]["refresh"] == wd.REFRESH_TOKEN
    assert len(data["sources"]) == 5
    assert live["state"].gui._world.calls == [], "the read route made no request"


def test_unknown_world_routes_are_404_and_refresh_is_validated(live):
    status, _ = _call_http(live["base"] + "/api/world/nonsense")
    assert status == 404
    status, _ = _call_http(live["base"] + "/api/world/overview/extra")
    assert status == 404
    status, body = _call_http(live["base"] + "/api/world/refresh", {})
    assert status == 400 and "REFRESH PUBLIC SOURCES" in body["error"]["message"]
    status, _ = _call_http(live["base"] + "/api/world/refresh", {})
    assert status == 400


def test_the_public_widget_routes_still_answer_after_the_desk_was_added(live):
    for path in ("/api/news", "/api/crypto", "/api/quakes"):
        status, body = _call_http(live["base"] + path)
        assert status == 200, path
        assert "items" in body or "data" in body, path


def test_the_desk_never_writes_a_headline_into_the_workspace(live):
    _call_http(live["base"] + "/api/world/overview")
    before = set((live["repo"]).glob("*.yaml")) | set((live["repo"] / "widget.yaml").read_text())
    _call_http(live["base"] + "/api/world/refresh", {"confirmation": wd.REFRESH_TOKEN, "groups": ["news"]})
    after = set((live["repo"]).glob("*.yaml")) | set((live["repo"] / "widget.yaml").read_text())
    assert before == after, "refreshing must not rewrite the configuration"

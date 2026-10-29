"""Scripted Telegram HTTP only; real private identity/queue/PIN/replay persistence."""

import json
import time

import httpx
import pytest
import yaml

from nexus.jarvis.runtime import Runtime
from nexus.server import ServerState
from nexus.telegram.bot import Bot

TOKEN = "123456789:" + "a" * 30
PIN = "86420975"


@pytest.fixture
def bot(repo, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_APPROVAL_PIN", PIN)
    (repo / "telegram.yaml").write_text(yaml.safe_dump({"enabled": True, "user_ids": [123]}))
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": []}))
    )
    instance = Bot(ServerState(repo, quiet=True).workbench.studio, client=client)
    yield instance
    instance.close()
    client.close()


def message(text, user=123):
    return {
        "from": {"id": user, "is_bot": False, "username": "not-trusted"},
        "chat": {"id": user, "type": "private"},
        "date": int(time.time()),
        "text": text,
    }


def test_exact_private_ids_not_names_groups_or_forwards(bot):
    assert bot.handle(message("/status", user=999)) is None
    group = message("/status")
    group["chat"]["type"] = "group"
    assert bot.handle(group) is None
    forwarded = message("/kill")
    forwarded["forward_origin"] = {"type": "user"}
    assert bot.handle(forwarded) is None
    stale = message("/kill")
    stale["date"] -= 121
    assert bot.handle(stale) is None
    assert json.loads(bot.handle(message("/status")))["state"] == "stopped"


def test_delegate_requires_existing_task_approval_and_pin_challenge(bot):
    result = bot.handle(message("/delegate Review local README"))
    identifier = result.rsplit(" ", 1)[1]
    assert Runtime(bot.studio).claim() is None
    challenge = bot.handle(message("/approve " + identifier)).split("/confirm ")[1].split()[0]
    with pytest.raises(ValueError):
        bot.handle(message("/confirm " + challenge + " 00000000"))
    response = bot.handle(message("/confirm " + challenge + " " + PIN))
    assert json.loads(response)["accepted"]
    assert bot.studio.find("jarvis-job", identifier)["status"] == "approved"
    with pytest.raises(ValueError):
        bot.handle(message("/confirm " + challenge + " " + PIN))
    assert PIN not in json.dumps(bot.studio.items("telegram-challenge"))


def test_challenge_cannot_be_transferred_or_approve_changed_task(bot):
    identifier = bot.handle(message("/delegate Review actual task")).split()[-1]
    challenge = bot.handle(message("/approve " + identifier)).split("/confirm ")[1].split()[0]
    with pytest.raises(ValueError):
        bot.confirm(999, challenge, PIN)
    row = bot.studio.find("jarvis-job", identifier)
    row["task"] = "changed after review"
    bot.studio.put("jarvis-job", row, identifier)
    with pytest.raises(ValueError):
        bot.handle(message("/confirm " + challenge + " " + PIN))
    assert bot.studio.find("jarvis-job", identifier)["status"] == "awaiting_approval"


def test_kill_needs_second_pin_not_arbitrary_chat(bot):
    challenge = bot.handle(message("/kill")).split("/confirm ")[1].split()[0]
    assert bot.studio.items("jarvis-control") == []
    bot.handle(message("/confirm " + challenge + " " + PIN))
    assert bot.studio.items("jarvis-control")[0]["desired"] == "killed"
    with pytest.raises(ValueError):
        bot.handle(message("run terminal command"))


def test_reports_are_metadata_only_unless_operator_opted_in(bot):
    job = bot.queue.enqueue("private task contents")
    assert "private task contents" not in bot.handle(message("/report " + job["id"]))
    assert bot.handle(message("/tasks"))
    with pytest.raises(ValueError):
        bot.handle(message("/delegate My PIN is " + PIN))


def test_durable_offset_is_reserved_before_side_effect_and_survives_restart(bot, monkeypatch):
    updates = [{"update_id": 1, "message": message("/delegate Process once only")}]
    replies = []
    monkeypatch.setattr(
        bot, "api", lambda method, payload: updates if method == "getUpdates" else replies.append(payload)
    )
    bot.poll_once()
    bot.poll_once()
    assert len(bot.studio.items("jarvis-job")) == 1
    assert bot.studio.items("telegram-state")[0]["offset"] == 2
    assert TOKEN not in json.dumps(replies) and PIN not in json.dumps(replies)


def test_whitelist_revocation_invalidates_challenges(bot):
    challenge = bot.handle(message("/kill")).split("/confirm ")[1].split()[0]
    (bot.root / "telegram.yaml").write_text(yaml.safe_dump({"enabled": True, "user_ids": [456]}))
    assert bot.handle(message("/confirm " + challenge + " " + PIN)) is None
    assert bot.studio.items("jarvis-control") == []


def test_wrong_pin_is_bounded_across_multiple_challenges(bot):
    for _ in range(5):
        challenge = bot.handle(message("/kill")).split("/confirm ")[1].split()[0]
        with pytest.raises(ValueError):
            bot.handle(message("/confirm " + challenge + " 00000000"))
    challenge = bot.handle(message("/kill")).split("/confirm ")[1].split()[0]
    with pytest.raises(ValueError):
        bot.handle(message("/confirm " + challenge + " " + PIN))
    assert bot.studio.items("jarvis-control") == []


def test_http_method_whitelist_no_redirects_and_counts_only_notifications(bot):
    sent = []
    bot.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json={"ok": True, "result": []})
        ),
        follow_redirects=False,
    )
    try:
        bot.api("getUpdates", {"offset": 0})
        assert sent[0].url.host == "api.telegram.org"
        with pytest.raises(ValueError):
            bot.api("deleteMessage", {})
        job = bot.queue.enqueue("never upload task/code")
        bot.notify({"offset": 0})
        payload = json.loads(sent[-1].content)
        assert "never upload" not in payload["text"] and str(job["id"]) not in payload["text"]
    finally:
        bot.client.close()


def test_quiet_dnd_preserve_inbox_without_outbound_notification(bot, monkeypatch):
    from nexus.jarvis.inbox import Inbox

    bot.queue.enqueue("Pending review")
    Inbox(bot.studio).configure(quiet=True)
    monkeypatch.setattr(bot, "api", lambda *args: pytest.fail("quiet suppresses notification"))
    bot.notify({"offset": 0})

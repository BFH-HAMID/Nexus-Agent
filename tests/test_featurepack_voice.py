"""The opt-in feature packages (Tasks 18-30) inside the real voice pipeline.

Each package has its own tests for what it answers. These tests are about the
wiring: where in a voice turn the packages are asked, what they are told about
the speaker and the session, and that nothing changes while they are OFF.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nexus import featurepacks
from nexus.computer_control import EmergencyStop
from nexus.voice.config import load_voice_config
from nexus.voice.pipeline import VoicePipeline, VoiceTurnResult
from nexus.voice.speaker import SpeakerMatch
from nexus.voice.stt import TranscriptResult

from .conftest import FakeProvider, fake_registry, text_completion
from .featurepack_helpers import scripted_agents, tool_registry, write_policy

AREAS = ("knowledge_graph", "council", "whatif", "explain", "presence", "hardware", "devpilot", "meetings", "translate",
         "docs_auto", "finance", "tutor")


class _STT:
    text, language = "", "en"

    def transcribe_stream(self, pcm, *, on_partial=None, language=None):
        return TranscriptResult(text=self.text, language=self.language)


class _TTS:
    extra_secrets: tuple = ()

    def __init__(self) -> None:
        self.clips: list[tuple[str, str]] = []

    def synthesize(self, text: str, *, language: str = "en", rate: float = 1.0):
        self.clips.append((text, language))
        return b"fake-pcm"


class _Player:
    def play(self, clip, interrupt_event=None):
        pass


class _Wake:
    def reset(self):
        pass

    def process_frame(self, frame):
        return None


class _VAD:
    chunk_samples = 512

    def speech_probability(self, pcm: bytes) -> float:
        return 1.0

    def reset(self):
        pass


class _Verifier:
    """A speaker check that says what the test tells it to say."""

    def __init__(self) -> None:
        self.owner = True

    def verify(self, audio) -> SpeakerMatch:
        return SpeakerMatch(matched=self.owner, score=0.9 if self.owner else 0.1, threshold=0.6, enrolled=True)


class Voice:
    """A voice pipeline with scripted ears and a recorded mouth; ``say`` is one spoken turn."""

    def __init__(self, repo: Path, *, verifier: _Verifier | None = None, require_always: bool = False,
                 speaker_enabled: bool | None = None) -> None:
        if speaker_enabled is None:
            speaker_enabled = verifier is not None
        (repo / "voice.yaml").write_text(yaml.safe_dump({
            "schema": 1, "enabled": True,
            "wake": {"enabled": False, "engine": "disabled"},
            "stt": {"language": "auto"},
            "speaker": {"enabled": speaker_enabled, "require_always": require_always, "threshold": 0.6},
            "duplex": {"enabled": False, "barge_in_threshold": 0.5, "barge_in_min_speech_ms": 200},
            "conversation": {"spoken_approvals": False, "resolve_references": False, "max_turns": 5,
                             "approval_timeout_seconds": 30},
            "acknowledgements": {"enabled": False, "min_delay_ms": 1000, "cooldown_seconds": 0, "max_per_minute": 0},
            "realtime": {"enabled": False, "provider": "local"},
        }), encoding="utf-8")
        self.stt, self.tts, self.verifier = _STT(), _TTS(), verifier
        self.asked: list[str] = []          # what reached the assistant (the model)

        def assistant(transcript: TranscriptResult) -> str:
            self.asked.append(transcript.text)
            return "the assistant answers"

        self.pipeline = VoicePipeline(
            load_voice_config(repo), workspace=repo, wake_detector=_Wake(), vad=_VAD(), stt=self.stt, tts=self.tts,
            player=_Player(), speaker_verifier=verifier, text_handler=assistant,
            persona=SimpleNamespace(
                enabled=False,
                language_policy=SimpleNamespace(mirror_user=True, allow_bangla_english_mixing=True, default_language="en"),
                spoken_style=SimpleNamespace(max_spoken_chars=10_000)))

    def turn(self, text: str, *, language: str = "en", owner: bool = True) -> VoiceTurnResult:
        self.stt.text, self.stt.language = text, language
        if self.verifier is not None:
            self.verifier.owner = owner
        return self.pipeline.process_frames([b"\xff" * 16384], skip_wake=True)

    def say(self, text: str, **how) -> tuple[str, str]:
        """``(what was spoken back, intent)``; the intent is empty when the assistant answered."""
        result = self.turn(text, **how)
        return result.response_text, result.approval_intent


def model(monkeypatch, config, name: str, *replies: str) -> FakeProvider:
    """Make ``name`` an agent of the workspace whose model answers with ``replies`` in turn.

    The pipeline gives a package no model of its own; a package loads the workspace's.
    """
    fake = FakeProvider(name, [text_completion(reply) for reply in replies])
    providers = scripted_agents(config, fake)
    monkeypatch.setattr("nexus.config.load_config", lambda workspace=None, *args, **kwargs: config)
    monkeypatch.setattr("nexus.policyfile.providers_for", lambda _config: providers)
    return fake


def capitals(repo: Path, **settings) -> None:
    from nexus.tutor.core import Tutor

    write_policy(repo, "tutor.yaml", voice={"enabled": True}, **settings)
    Tutor(repo).add_many("capitals", [("", "France", "Paris"), ("", "Japan", "Tokyo")])


def family(repo: Path) -> None:
    from nexus.knowledge_graph.graph import KnowledgeGraph

    write_policy(repo, "knowledge_graph.yaml", encrypt_at_rest=False)
    KnowledgeGraph(repo).ingest("My sister is Ayesha. Ayesha works at Grameenphone. Rahim is Ayesha's manager.",
                                source_kind="user", source_ref="t1")


def translation(text: str) -> str:
    return json.dumps({"translation": text}, ensure_ascii=False)


# ── OFF means unchanged ────────────────────────────────────────────────── #


PHRASES = ("who is Ayesha", "how is Rahim related to me", "ask the council whether to deploy", "what if I run rm -rf build",
           "why did you do that?", "I'm home", "am I home?", "everyone agreed, start meeting notes", "summarize the meeting",
           "translate good morning to Bengali", "start interpreter", "how much did I spend last month", "am I over budget",
           "quiz me on capitals", "stop the quiz")


def test_while_every_package_is_off_the_assistant_hears_everything(repo: Path):
    voice = Voice(repo)
    for phrase in PHRASES:
        assert voice.say(phrase) == ("the assistant answers", ""), phrase
    assert voice.asked == list(PHRASES)
    assert [clip for clip, _language in voice.tts.clips] == ["the assistant answers"] * len(PHRASES)
    home = Path(os.environ["NEXUS_HOME"])
    assert not [area for area in AREAS if (home / area).exists()]


def test_a_package_answers_only_while_its_own_policy_is_on(repo: Path):
    voice = Voice(repo)
    write_policy(repo, "presence.yaml", debounce_seconds=0)
    assert voice.say("I'm home") == ("Welcome home.", "presence_set")
    assert voice.say("how much did I spend last month") == ("the assistant answers", "")
    write_policy(repo, "presence.yaml", enabled=False)
    assert voice.say("I'm home") == ("the assistant answers", "")
    assert voice.asked == ["how much did I spend last month", "I'm home"]


# ── every package through the pipeline ─────────────────────────────────── #


def test_what_a_package_says_is_spoken_and_never_reaches_the_model(repo: Path):
    write_policy(repo, "presence.yaml", debounce_seconds=0)
    voice = Voice(repo)
    result = voice.turn("I'm leaving.")
    assert (result.response_text, result.approval_intent, result.error) == ("Okay, marked as away.", "presence_set", "")
    assert result.spoken_sentences == ["Okay, marked as away."] and voice.tts.clips == [("Okay, marked as away.", "en")]
    assert result.states[-2:] == ["speaking", "idle"] and result.reply_language == "en"
    assert voice.asked == [] and len(voice.pipeline.conversation.turns) == 0
    assert voice.say("am I home?") == ("You are marked away (away set manually).", "presence_status")
    # a sentence no package knows is the assistant's, as before
    assert voice.say("what is the weather like") == ("the assistant answers", "") and voice.asked == ["what is the weather like"]


def test_knowledge_graph_explain_and_whatif_by_voice(repo: Path):
    family(repo)
    write_policy(repo, "explain.yaml")
    write_policy(repo, "whatif.yaml")
    _ctx, reg = tool_registry(repo, None, mode="auto")
    reg.call("delete_file", {"path": "x.txt"})
    voice = Voice(repo)
    reply, intent = voice.say("Who is Ayesha?")
    assert intent == "kg_about" and "sister" in reply and "Grameenphone" in reply
    reply, intent = voice.say("how is Rahim related to me?")
    assert intent == "kg_path" and "manager" in reply
    assert voice.say("who is Albert Einstein") == ("the assistant answers", "")       # not in the graph: the assistant's
    reply, intent = voice.say("Why did you do that?")
    assert intent == "explain_last" and "delete_file" in reply
    reply, intent = voice.say("what if I run rm -rf dist")
    assert intent == "whatif" and "deletes files recursively" in reply
    assert voice.say("what if we moved the company to Mars") == ("the assistant answers", "")
    assert voice.asked == ["who is Albert Einstein", "what if we moved the company to Mars"]


def test_meetings_and_finance_by_voice(repo: Path):
    write_policy(repo, "meetings.yaml", encrypt_at_rest=False)
    write_policy(repo, "finance.yaml", encrypt_at_rest=False, voice=True)
    voice = Voice(repo)
    assert voice.say("start meeting notes for the budget review")[1] == "meeting_consent"
    assert voice.say("Everyone agreed, start meeting notes for the budget review.")[1] == "meeting_start"
    assert voice.say("meeting note: Karim will send the budget file by Monday.") == ("Noted, 1 so far.", "meeting_note")
    assert voice.say("what is the weather like") == ("the assistant answers", "")      # other speech is not written down
    assert voice.say("stop meeting notes")[1] == "meeting_stop"
    reply, intent = voice.say("summarize the meeting")
    assert intent == "meeting_summary" and "Karim will send the budget file by Monday." in reply
    assert voice.say("How much did I spend last month?") == (
        "I have no transactions yet. Import a statement first.", "finance_no_data")
    assert voice.asked == ["what is the weather like"]


def test_a_translation_is_spoken_in_its_own_language(repo: Path, config, monkeypatch):
    fake = model(monkeypatch, config, "lingo", translation("শুভ সকাল"), translation("Buenos días"))
    write_policy(repo, "translate.yaml", agent="lingo")
    voice = Voice(repo)
    result = voice.turn("Translate good morning to Bengali.")
    assert (result.response_text, result.approval_intent, result.reply_language) == ("শুভ সকাল", "translate", "bn")
    assert voice.tts.clips == [("শুভ সকাল", "bn")]                                       # not in the voice of the question
    assert voice.say("how do you say good morning in Spanish") == ("Buenos días", "translate")
    assert voice.say("translate this into something nice") == ("the assistant answers", "")
    assert len(fake.calls) == 2 and voice.asked == ["translate this into something nice"]


def test_a_spoken_quiz_through_the_pipeline(repo: Path):
    capitals(repo)
    voice = Voice(repo)
    assert voice.say("Quiz me on capitals.") == (
        "Quiz, 2 questions. Say 'stop the quiz' to end. Question 1: France", "tutor_start")
    assert voice.say("I think it's Paris") == ("Right. Question 2: Japan", "tutor_answer")
    assert voice.say("Kyoto") == ("The card says: Tokyo. Were you right? Say yes or no.", "tutor_unsure")
    assert voice.say("no") == ("Okay. Question 3: Japan", "tutor_answer")
    assert voice.say("Tokyo") == ("Right. Quiz finished: 2 of 3 right.", "tutor_answer")
    assert voice.say("Paris") == ("the assistant answers", "")                           # the quiz is over
    assert voice.asked == ["Paris"] and len(voice.pipeline.conversation.turns) == 1


# ── a mode that owns every utterance ───────────────────────────────────── #


def test_while_the_interpreter_runs_every_sentence_is_translated(repo: Path, config, monkeypatch):
    fake = model(monkeypatch, config, "lingo", *(translation(text) for text in (
        "আপনার পাসপোর্ট আনতে ভুলবেন না।", "I am fine, thank you.", "অবস্থা কী?", "মিশন বাতিল করুন", "গোপনীয়তা মোড চালু")))
    write_policy(repo, "translate.yaml", agent="lingo", interpreter={"enabled": True})
    voice = Voice(repo)
    assert voice.say("remember to bring your passport")[1] == "memory_blocked"           # the memory command, as before
    assert voice.say("start interpreter")[1] == "translate_start"
    # sentences that are commands of the pipeline at other times are now part of the conversation
    assert voice.say("Remember to bring your passport.") == ("আপনার পাসপোর্ট আনতে ভুলবেন না।", "translate_interpreter")
    result = voice.turn("আমি ভালো আছি, ধন্যবাদ।", language="bn")
    assert (result.response_text, result.reply_language) == ("I am fine, thank you.", "en")
    assert voice.tts.clips[-1] == ("I am fine, thank you.", "en")                        # English, for the English speaker
    assert voice.say("What is the status?") == ("অবস্থা কী?", "translate_interpreter")
    assert voice.say("cancel the mission") == ("মিশন বাতিল করুন", "translate_interpreter")
    assert voice.say("privacy mode on") == ("গোপনীয়তা মোড চালু", "translate_interpreter")
    assert voice.pipeline.long_term_memory.voice_privacy_mode is False and voice.asked == []
    assert len(fake.calls) == 5
    assert voice.say("Stop interpreter.") == ("Interpreter stopped.", "translate_stop")
    assert voice.say("What is the status?")[1] == "chief_status"                         # and the pipeline's again


def test_the_emergency_stop_is_heard_in_any_mode(repo: Path):
    capitals(repo)
    voice = Voice(repo)
    EmergencyStop().set(False)
    try:
        assert voice.say("quiz me")[1] == "tutor_start"
        result = voice.turn("Nexus, stop everything")
        assert result.emergency_stop is True and EmergencyStop().stopped() is True
        assert featurepacks.status(repo)["tutor"]["session"] == "quiz (voice)"           # it was not taken for an answer
        assert voice.say("Paris") == ("Right. Question 2: Japan", "tutor_answer")
    finally:
        EmergencyStop().set(False)


def test_in_a_quiz_an_answer_is_an_answer_whatever_it_sounds_like(repo: Path):
    from nexus.tutor.core import Tutor

    write_policy(repo, "tutor.yaml", voice={"enabled": True})
    Tutor(repo).add_many("history", [("", "The Texan battle cry of 1836", "Remember the Alamo"),
                                     ("", "A small blue flower", "forget-me-not"),
                                     ("", "Said to a waiter", "what is the status of my order")])
    voice = Voice(repo)
    assert voice.say("test me on history")[1] == "tutor_start"
    assert voice.say("Remember the Alamo!") == ("Right. Question 2: A small blue flower", "tutor_answer")
    assert voice.say("forget me not") == ("The card says: forget-me-not. Were you right? Say yes or no.", "tutor_unsure")
    assert voice.say("yes") == ("Counted as right. Question 3: Said to a waiter", "tutor_answer")
    assert voice.say("What is the status of my order?") == ("Right. Quiz finished: 3 of 3 right.", "tutor_answer")
    assert voice.asked == []


# ── memory keeps its commands ──────────────────────────────────────────── #


def test_memory_commands_stay_with_memory_and_step_aside_only_while_memory_is_off(repo: Path):
    family(repo)
    voice = Voice(repo)
    # long-term memory is off here: its "disabled" answer does not hide a package that knows the sentence
    reply, intent = voice.say("What do you know about Ayesha?")
    assert intent == "kg_about" and "sister" in reply
    assert voice.say("remember that Rahim knows Karim") == ("Noted: Rahim knows Karim.", "kg_add")
    # a memory command no package knows gets memory's own answer, as before
    reply, intent = voice.say("what do you remember about Ayesha")
    assert intent == "memory_blocked" and "memory" in reply.lower()
    assert voice.say("what do you know about quantum physics")[1] == "memory_blocked"    # not in the graph either
    assert voice.asked == []
    # with memory on, the same sentences are memory's
    (repo / "memory.yaml").write_text(yaml.safe_dump({
        "schema": 1, "enabled": True, "profile": {"enabled": True}, "privacy": {"encrypt_at_rest": False}}), encoding="utf-8")
    remembering = Voice(repo)
    assert remembering.pipeline.long_term_memory.enabled is True
    reply, intent = remembering.say("what do you know about Ayesha")
    assert intent == "recall" and not reply.startswith("About Ayesha")
    assert remembering.say("who is Ayesha")[1] == "kg_about"                             # the graph's own sentence


# ── who is speaking, and what the session has read ─────────────────────── #


def test_a_package_is_told_whether_the_speaker_was_verified(repo: Path, monkeypatch):
    write_policy(repo, "finance.yaml", encrypt_at_rest=False, voice=True)
    capitals(repo)
    family(repo)
    seen = []
    voice = Voice(repo, verifier=_Verifier())
    original = featurepacks.handle_text

    def spy(workspace, text, ctx=None, **options):
        seen.append(ctx["speaker_verified"])
        return original(workspace, text, ctx, **options)

    monkeypatch.setattr(featurepacks, "handle_text", spy)
    assert voice.say("how much did I spend last month", owner=False) == (
        "I only talk about your money on a trusted, verified request.", "finance_refused")
    assert voice.say("who is Ayesha", owner=False) == (
        "I only say what the graph holds on a trusted, verified request.", "kg_refused")
    assert voice.say("quiz me", owner=False) == ("I only run a quiz on a trusted, verified request.", "tutor_refused")
    assert set(seen) == {False}
    assert voice.say("how much did I spend last month")[1] == "finance_no_data" and seen[-1] is True
    assert voice.say("quiz me")[1] == "tutor_start"
    assert voice.say("Paris", owner=False)[1] == "tutor_refused"                          # somebody else answers
    assert voice.say("stop the quiz", owner=False)[1] == "tutor_refused"                  # or tries to end it
    assert voice.say("Paris") == ("Right. Question 2: Japan", "tutor_answer")
    assert voice.say("stop the quiz")[1] == "tutor_stop"
    # without a speaker check nobody can be told apart, and the package is told that too
    seen.clear()
    assert Voice(repo).say("who is Ayesha")[1] == "kg_about" and set(seen) == {None}
    assert voice.asked == []


class _Counting(_Verifier):
    def __init__(self) -> None:
        super().__init__()
        self.compared = 0
        self.broken = False

    def verify(self, audio) -> SpeakerMatch:
        self.compared += 1
        if self.broken:
            raise RuntimeError("no encoder")
        return super().verify(audio)


def test_the_voice_is_compared_only_when_a_package_asks(repo: Path, monkeypatch):
    """``nexus voice run`` builds the pipeline without a verifier; the enrolled voiceprint is used on demand."""
    write_policy(repo, "presence.yaml", debounce_seconds=0)
    voice = Voice(repo)
    verifier = _Counting()
    monkeypatch.setattr(VoicePipeline, "active_speaker_verifier", lambda self: verifier)
    assert voice.say("what is the weather like") == ("the assistant answers", "") and verifier.compared == 0
    assert voice.say("am I home?")[1] == "presence_status" and verifier.compared == 0     # nothing to protect
    assert voice.say("I'm home")[1] == "presence_set" and verifier.compared == 1          # once, not once per stage
    verifier.owner = False
    assert voice.say("I'm leaving")[1] == "presence_refused" and verifier.compared == 2
    assert voice.say("am I home?") == ("You are marked present (home set manually).", "presence_status")
    # a comparison that cannot be made counts as failed, as it does for a spoken approval
    verifier.owner, verifier.broken = True, True
    assert voice.say("I'm leaving")[1] == "presence_refused" and verifier.compared == 3
    # the approval gate goes on deciding by its own record
    assert voice.pipeline.last_speaker_match is None


def test_a_speaker_check_that_is_enabled_without_a_voiceprint_verifies_nobody(repo: Path):
    write_policy(repo, "presence.yaml", debounce_seconds=0)
    voice = Voice(repo)
    assert voice.pipeline._speaker_verdict(b"pcm") is None                                # no check is in use
    assert voice.say("I'm home")[1] == "presence_set"
    enabled = Voice(repo, speaker_enabled=True)                                           # nobody has enrolled
    assert enabled.pipeline.config.speaker.enabled is True and enabled.pipeline.speaker_verifier is None
    assert enabled.pipeline._speaker_verdict(b"pcm") is False
    assert enabled.say("I'm leaving")[1] == "presence_refused"
    assert enabled.say("am I home?")[1] == "presence_status"


def test_with_require_always_an_unverified_voice_reaches_no_package(repo: Path):
    capitals(repo)
    voice = Voice(repo, verifier=_Verifier(), require_always=True)
    assert voice.say("quiz me")[1] == "tutor_start"
    result = voice.turn("Paris", owner=False)
    assert result.error == "Speaker verification failed" and result.response_text == "" and voice.tts.clips[1:] == []
    assert voice.say("Paris") == ("Right. Question 2: Japan", "tutor_answer")


def test_after_the_assistant_read_untrusted_content_a_package_is_told(repo: Path):
    write_policy(repo, "finance.yaml", encrypt_at_rest=False, voice=True)
    write_policy(repo, "presence.yaml", debounce_seconds=0)
    voice = Voice(repo)
    assert voice.pipeline.active_gate is None
    assert voice.say("how much did I spend last month")[1] == "finance_no_data"           # nothing was read yet
    voice.pipeline.active_gate = SimpleNamespace(tainted=True)       # as build_agent_handler leaves it after such a turn
    assert voice.say("how much did I spend last month")[1] == "finance_refused"
    assert voice.say("I'm home") == ("I can only change presence for a verified speaker in a trusted session.",
                                     "presence_refused")
    assert voice.say("am I home?")[1] == "presence_status"                                # a question changes nothing
    voice.pipeline.active_gate = SimpleNamespace(tainted=False)      # the assistant's next turn read nothing untrusted
    assert voice.say("I'm home")[1] == "presence_set"


def test_the_assistants_turn_leaves_its_gate_for_the_packages(repo: Path, config, monkeypatch):
    from nexus.voice.pipeline import build_agent_handler

    provider = FakeProvider("gemini", [text_completion("hello")])
    config.default_agent = "gemini"
    monkeypatch.setattr("nexus.config.load_config", lambda workspace=None, *args, **kwargs: config)
    monkeypatch.setattr("nexus.agent.build_providers", lambda *args, **kwargs: fake_registry(provider, config=config))
    voice = Voice(repo)
    handler = build_agent_handler(repo, pipeline=voice.pipeline, stream=False)
    assert handler(TranscriptResult(text="hello there", language="en")) == "hello"
    gate = voice.pipeline.active_gate
    assert gate is not None and gate.tainted is False
    assert voice.pipeline._feature_context()["tainted"] is False
    gate.mark_tainted("web_fetch")
    assert voice.pipeline._feature_context()["tainted"] is True


# ── voice privacy mode ─────────────────────────────────────────────────── #


def test_in_voice_privacy_mode_stored_personal_data_is_not_spoken(repo: Path):
    write_policy(repo, "finance.yaml", encrypt_at_rest=False, voice=True)
    write_policy(repo, "meetings.yaml", encrypt_at_rest=False)
    family(repo)
    capitals(repo)
    voice = Voice(repo)
    assert voice.say("everyone agreed, start meeting notes")[1] == "meeting_start"
    assert voice.say("meeting note: Karim will send the budget file by Friday.")[1] == "meeting_note"
    assert voice.say("privacy mode on")[1] == "privacy_mode_on"
    for phrase, intent, secret in (("who is Ayesha", "kg_private", "sister"),
                                   ("how is Rahim related to me", "kg_private", "manager"),
                                   ("how much did I spend last month", "finance_private", "transactions"),
                                   ("am I over budget", "finance_private", "budget"),
                                   ("summarize the meeting", "meeting_private", "Friday")):
        reply, got = voice.say(phrase)
        assert got == intent and reply.startswith("Voice privacy mode is active") and secret not in reply, phrase
    # what you tell it is still taken, without being repeated
    assert voice.say("note that Rahim knows Karim") == ("Noted.", "kg_add")
    assert voice.say("meeting note: Rahim will book the room on Tuesday.") == ("Noted, 2 so far.", "meeting_note")
    assert voice.say("stop meeting notes")[1] == "meeting_stop"
    assert voice.say("who is Albert Einstein") == ("the assistant answers", "")           # not in the graph
    # a quiz is something you ask to hear
    assert voice.say("quiz me")[1] == "tutor_start" and voice.say("stop the quiz")[1] == "tutor_stop"
    assert voice.say("privacy mode off")[1] == "privacy_mode_off"
    assert voice.say("who is Ayesha")[1] == "kg_about"
    reply, intent = voice.say("summarize the meeting")
    assert intent == "meeting_summary" and "Friday" in reply


def test_when_the_privacy_setting_cannot_be_read_nothing_stored_is_spoken(repo: Path):
    write_policy(repo, "finance.yaml", encrypt_at_rest=False, voice=True)
    voice = Voice(repo)
    (repo / "memory.yaml").write_text("schema: 1\nenabled: [unclosed\n", encoding="utf-8")
    assert voice.pipeline._feature_context()["privacy_mode"] is True
    assert voice.pipeline._memory_enabled() is True


# ── a package that fails ───────────────────────────────────────────────── #


def test_a_failing_package_says_so_or_steps_aside_and_the_turn_goes_on(repo: Path, monkeypatch, capsys):
    write_policy(repo, "presence.yaml", debounce_seconds=0)
    voice = Voice(repo)

    def broken(error):
        def handle(workspace, text, ctx=None):
            raise error
        return handle

    monkeypatch.setattr("nexus.presence.commands.handle", broken(ValueError("the store is damaged")))
    assert voice.say("I'm home") == ("Home presence is unavailable: the store is damaged", "presence_error")
    monkeypatch.setattr("nexus.presence.commands.handle", broken(AttributeError("a bug")))
    assert voice.say("I'm home") == ("the assistant answers", "")
    assert "voice: an opt-in feature failed and was skipped - AttributeError: a bug" in capsys.readouterr().err
    assert voice.asked == ["I'm home"]


# ── featurepacks.handle_text ───────────────────────────────────────────── #


def test_a_context_works_a_value_out_once_and_only_when_it_is_read():
    calls = []
    ctx = featurepacks.Context({"source": "voice"}, lazy={"speaker_verified": lambda: calls.append(1) or False})
    assert ctx and "speaker_verified" in ctx and "nothing" not in ctx and ctx.get("nothing", 7) == 7
    assert ctx["source"] == "voice" and calls == []
    assert ctx.get("speaker_verified") is False and ctx["speaker_verified"] is False and calls == [1]
    assert dict(ctx) == {"source": "voice", "speaker_verified": False}
    with pytest.raises(KeyError):
        ctx["nothing"]
    # a context that holds only values still to be worked out is not "no context"
    assert featurepacks.Context(lazy={"speaker_verified": lambda: True}) and not featurepacks.Context()


def test_handle_text_can_ask_only_the_packages_that_own_the_conversation(repo: Path):
    capitals(repo)
    write_policy(repo, "presence.yaml", debounce_seconds=0)
    assert featurepacks.handle_text(repo, "I'm home", modal=True) is None                 # nothing owns the conversation
    assert featurepacks.handle_text(repo, "quiz me", modal=True) is None
    assert featurepacks.handle_text(repo, "quiz me", modal=False)[1] == "tutor_start"
    # now the quiz does: asked alone it takes every sentence, and in one call it is asked first
    assert featurepacks.handle_text(repo, "I'm home", modal=True)[1] == "tutor_unsure"
    assert featurepacks.handle_text(repo, "no", modal=True)[1] == "tutor_answer"
    assert featurepacks.handle_text(repo, "I'm home")[1] == "tutor_unsure"
    assert featurepacks.handle_text(repo, "no")[1] == "tutor_answer"
    assert featurepacks.handle_text(repo, "stop the quiz")[1] == "tutor_stop"
    assert featurepacks.handle_text(repo, "I'm home")[1] == "presence_set"
    for nothing in ("", "   ", "x" * 2001):
        assert featurepacks.handle_text(repo, nothing) is None


SENTENCES = {
    "knowledge_graph": ("who is Ayesha", "what do you know about Ayesha", "how is Rahim related to me",
                        "note that Rahim knows Karim"),
    "council": ("ask the council whether to deploy",),
    "whatif": ("what if I run rm -rf build", "what happens if I delete data.csv"),
    "explain": ("why did you do that?", "explain that", "what did you just do"),
    "presence": ("I'm home", "I'm leaving", "am I home?"),
    "meetings": ("start meeting notes", "everyone agreed, start meeting notes", "meeting note: the launch is on Friday",
                 "stop meeting notes", "summarize the meeting"),
    "translate": ("translate good morning to Bengali", "how do you say thank you in Bangla", "start interpreter",
                  "stop interpreter"),
    "finance": ("how much did I spend last month", "am I over budget", "budget status"),
    "tutor": ("quiz me on capitals", "test me", "start a quiz", "stop the quiz", "repeat the question", "skip"),
}
SWITCHES = {
    "knowledge_graph": {"encrypt_at_rest": False},
    "whatif": {},
    "explain": {},
    "presence": {"debounce_seconds": 0},
    "meetings": {"encrypt_at_rest": False},
    "translate": {"interpreter": {"enabled": True}},
    "finance": {"encrypt_at_rest": False, "voice": True},
    "tutor": {"voice": {"enabled": True}},
}


@pytest.mark.parametrize("key", sorted(SWITCHES))
def test_a_package_leaves_the_sentences_of_the_others_alone(repo: Path, key: str):
    """Each documented sentence has one owner: a package that is ON alone does not answer another's."""
    feature = featurepacks.by_key(key)
    write_policy(repo, feature.policy, **SWITCHES[key])
    assert featurepacks.status(repo)[key]["enabled"] is True
    for other, sentences in SENTENCES.items():
        if other == key:
            continue
        for sentence in sentences:
            assert featurepacks.handle_text(repo, sentence, {"speaker_verified": True}) is None, (key, sentence)

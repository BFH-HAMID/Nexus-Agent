"""``/tutor`` in the REPL, and the spoken quiz (voice pipeline). Nothing here calls a model by itself."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import policy

OFF = "Tutor is OFF (operator opt-in: tutor.yaml enabled: true)."
USAGE = ("Usage: /tutor [stats [DECK]] | decks | quiz [DECK] [N] | review [DECK] [N] | a ANSWER | yes | no | skip | "
         "show | again | hard | good | easy | stop | add DECK :: QUESTION :: ANSWER")
REFUSED = "I only run a quiz on a trusted, verified request."

# Each pattern is anchored at both ends and holds bounded repetitions only; it is tried on one utterance.
_START = re.compile(r"^(?:please )?(?:quiz me|test me|(?:start|begin|give me) (?:a |the )?quiz|let'?s (?:do|start) a quiz)"
                    r"(?: (?:on|about|in|with) (?:my |the )?(?P<deck>.{1,40}?)(?: deck| cards)?)?[.!?]?$", re.I)
_STOP = re.compile(r"^(?:please )?(?:stop|end|quit|finish|cancel|exit) (?:the |this )?(?:quiz|test)[.!]?$", re.I)
_REPEAT = re.compile(r"^(?:please )?(?:repeat(?: (?:that|it|the question))?|say (?:that|it) again|"
                     r"what (?:was|is) the question|again)[.!?]?$", re.I)
_SKIP = re.compile(r"^(?:skip(?: (?:it|this one|that one|this|that))?|pass|next(?: one| question)?|"
                   r"i (?:do not|don'?t) know|no idea|i give up)[.!]?$", re.I)
_YES = re.compile(r"^(?:yes|yeah|yep|yup|correct|right|i was(?: right)?|that'?s right|yes i was)[.!]?$", re.I)
_NO = re.compile(r"^(?:no|nope|nah|wrong|i was wrong|i wasn'?t|not really|no i wasn'?t)[.!]?$", re.I)
# "um, I think it's Paris" -> "Paris": at most one of each, in this order
_LEAD = re.compile(r"^(?:(?:um{1,5}|uh{1,5}|er{1,5}|well|okay|ok|so)[, ]{1,3})?(?:(?:i think|i guess|i believe|maybe)[, ]{1,3})?"
                   r"(?:(?:it'?s|it is|that'?s|that is|the answer is|is it)[, ]{1,3})?", re.I)


def count(text: str, default: int | None = None) -> int | None:
    """A typed number of cards; anything that is not a short plain number gives ``default``."""
    text = str(text or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 3 else default


def _deck_and_count(tutor, rest: str) -> tuple[str, int | None]:
    """``spanish verbs 5`` -> ``("spanish verbs", 5)``; a deck whose own name ends in a number wins."""
    rest = " ".join(rest.split())
    head, _, last = rest.rpartition(" ")
    number = count(last)
    if number is None or rest.casefold() in {row["deck"] for row in tutor.decks()}:
        return rest, None
    return head, number


def _next(result: dict[str, Any], spoken: bool = False) -> str:
    """What comes after an answer: the next question, or the summary."""
    from .core import question_line, summary_line

    if result["summary"] is not None:
        return summary_line(result["summary"])
    view = result["next"]
    return f"Question {view['number']}: {view['front']}" if spoken else question_line(view)


def _after(result: dict[str, Any]) -> str:
    from .core import outcome_line

    if result["result"] == "unsure":
        return outcome_line(result) + " /tutor yes | /tutor no"
    return outcome_line(result) + "\n" + _next(result)


def slash(workspace: Path | str, argument: str, ctx: dict[str, Any] | None = None) -> str:
    """``/tutor``: statistics, and a quiz or review that goes on from one command to the next."""
    if not policy.enabled(workspace):
        return OFF
    from .cards import one_line
    from .core import Tutor, printable, question_line, stats_lines, summary_line

    ctx = ctx or {}
    tutor = Tutor(workspace, ctx.get("config"), ctx.get("providers"), keyring_module=ctx.get("keyring_module"))
    verb, _, rest = argument.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb in ("", "stats"):
        lines = stats_lines(tutor.stats(rest))
        view = tutor.current()
        if view is not None:
            lines.append(f"{view['kind']} running: {question_line(view)}")
        return "\n".join(lines)
    if verb == "decks":
        return "\n".join(f"{row['deck']}: {row['cards']} card(s), {row['due']} due, {row['new']} new"
                         for row in tutor.decks()) or "no cards yet: /tutor add DECK :: QUESTION :: ANSWER"
    if verb in ("quiz", "review"):
        deck, number = _deck_and_count(tutor, rest)
        started = tutor.begin(verb, deck, number, origin="slash")
        how = "answer: /tutor a YOUR ANSWER   (skip: /tutor skip, end: /tutor stop)" if verb == "quiz" else \
            "see the answer: /tutor show   then: /tutor again | hard | good | easy   (end: /tutor stop)"
        return "\n".join(part for part in (started["note"], question_line(started["question"]), how) if part)
    if verb in ("a", "answer"):
        return _after(tutor.answer(rest))
    if verb in ("yes", "y", "no", "n"):
        return _after(tutor.settle(verb in ("yes", "y")))
    if verb == "skip":
        return _after(tutor.skip())
    if verb == "show":
        return printable(tutor.reveal()["back"]) + "\nHow well did you know it? /tutor again | hard | good | easy"
    if verb in policy.GRADES:
        return _after(tutor.grade(verb))
    if verb == "stop":
        summary = tutor.stop()
        return summary_line(summary, stopped=True) if summary else "No quiz or review was running."
    if verb == "add":
        parts = [part.strip() for part in rest.split(" :: ")]
        if len(parts) != 3 or not all(parts):
            return "Usage: /tutor add DECK :: QUESTION :: ANSWER"
        done = tutor.add(parts[0], parts[1], parts[2], by="slash")
        return f"added to '{done['deck']}': {printable(one_line(parts[1], 80))}"
    return USAGE


# --------------------------------------------------------------------------- #
# voice
# --------------------------------------------------------------------------- #


def exclusive(workspace: Path | str) -> bool:
    """True while a spoken quiz is running: what is said next is an answer."""
    if not policy.enabled(workspace, "voice.enabled"):
        return False
    try:
        from .core import Tutor

        view = Tutor(workspace).current()
    except (ValueError, OSError):
        return False
    return view is not None and view["origin"] == "voice"


def _said(text: str) -> tuple[str, ...]:
    """Other readings of a spoken answer: without "um, I think it's"."""
    bare = text[_LEAD.match(text).end():].strip()
    return (bare,) if bare and bare != text else ()


def handle(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """``(reply, intent)`` for "quiz me on DECK", and for every utterance while a spoken quiz is running."""
    if not policy.enabled(workspace, "voice.enabled"):
        return None
    from .core import Tutor, summary_line

    ctx = ctx or {}
    spoken = " ".join(str(text or "").split())
    if not spoken or len(spoken) > 2000:
        return None
    start = _START.match(spoken)
    try:
        tutor = Tutor(workspace, ctx.get("config"), ctx.get("providers"), keyring_module=ctx.get("keyring_module"))
        view = tutor.current()
        if view is not None and view["origin"] != "voice":
            view = None     # a typed session is not answered by voice
        if view is None and not start:
            return None
        if ctx.get("tainted") or ctx.get("speaker_verified") is False:
            return REFUSED, "tutor_refused"
        if view is not None and tutor.fits(spoken, also=_said(spoken)):
            # the card's answer comes before the words below: it may be "pass", "again" or "next"
            return "Right. " + _next(tutor.answer(spoken, also=_said(spoken)), True), "tutor_answer"
        if _STOP.match(spoken) and view is not None:
            summary = tutor.stop()
            return (summary_line(summary, stopped=True) if summary else "The quiz was over."), "tutor_stop"
        if start:
            started = tutor.begin("quiz", (start.group("deck") or "").strip(" '\""), origin="voice")
            first = started["question"]
            return (f"Quiz, {started['cards']} question{'s' if started['cards'] != 1 else ''}. Say 'stop the quiz' to "
                    f"end. Question 1: {first['front']}"), "tutor_start"
        if _REPEAT.match(spoken):
            if view["pending"]:
                return f"The card says: {view['back']}. Were you right? Say yes or no.", "tutor_repeat"
            return f"Question {view['number']}: {view['front']}", "tutor_repeat"
        if view["pending"]:
            if not (_YES.match(spoken) or _NO.match(spoken)):
                return "Say yes if your answer was right, or no.", "tutor_unclear"
            result = tutor.settle(bool(_YES.match(spoken)))
            return ("Counted as right. " if result["result"] == "right" else "Okay. ") + _next(result, True), "tutor_answer"
        if _SKIP.match(spoken):
            result = tutor.skip()
            return f"The answer is: {result['expected']}. " + _next(result, True), "tutor_answer"
        result = tutor.answer(spoken, also=_said(spoken))
        if result["result"] == "unsure":
            return f"The card says: {result['expected']}. Were you right? Say yes or no.", "tutor_unsure"
        return "Right. " + _next(result, True), "tutor_answer"
    except (ValueError, OSError) as exc:
        return f"Tutor: {str(exc)[:200]}", "tutor_error"
